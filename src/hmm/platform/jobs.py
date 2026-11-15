import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Concatenate, cast

import httpx
import procrastinate
from fastapi import Depends, Request
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.job_context import JobContext
from psycopg import AsyncConnection
from pydantic import JsonValue
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hmm.algo.models import AlgoContext, Algorithm
from hmm.platform import settings
from hmm.platform.algo import DatabaseAlgoCache
from hmm.platform.clock import Clock
from hmm.platform.config import Config
from hmm.platform.storage import ObjectStore


class JobName(StrEnum):
    GITHUB_SYNC = "github_sync"
    GITHUB_SYNC_ALL = "github_sync_all"
    CV_EXTRACT = "cv_extract"
    SCORE_ONBOARDING = "score_onboarding"
    RECOMPUTE_MEMBER = "recompute_member"
    RECOMPUTE_ALL_MEMBERS = "recompute_all_members"
    REFRESH_RECOMMENDATIONS = "refresh_recommendations"
    RUN_FORMATION = "run_formation"
    MATCH_MENTORS = "match_mentors"
    SCAN_COMMUNITY = "scan_community"
    COMMUNITY_TICK = "community_tick"
    EVENT_TICK = "event_tick"
    ADMIN_DIGEST = "admin_digest"
    ARCHIVE_OLD_EVENTS = "archive_old_events"
    CLEANUP = "cleanup"


RETRY_POLICY = procrastinate.RetryStrategy(max_attempts=5, exponential_wait=5)

JOB_MODULES: tuple[str, ...] = (
    "hmm.platform.maintenance",
    "hmm.admin.jobs",
    "hmm.catalog.jobs",
    "hmm.onboarding.jobs",
    "hmm.profiles.jobs",
    "hmm.scores.jobs",
    "hmm.formation.jobs",
    "hmm.recommendations.jobs",
    "hmm.mentorship.jobs",
    "hmm.ticks",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSpec:
    lock: str | None = None
    queueing_lock: str | None = None
    cron: str | None = None
    timeout: float = 900


def singleton(name: JobName, cron: str, timeout: float = 900) -> JobSpec:
    return JobSpec(lock=name.value, queueing_lock=name.value, cron=cron, timeout=timeout)


JOBS: dict[JobName, JobSpec] = {
    JobName.GITHUB_SYNC: JobSpec(lock="user:{user_id}", queueing_lock="github_sync:{user_id}"),
    JobName.GITHUB_SYNC_ALL: singleton(JobName.GITHUB_SYNC_ALL, "0 3 * * *"),
    JobName.CV_EXTRACT: JobSpec(lock="user:{user_id}", queueing_lock="cv_extract:{user_id}"),
    JobName.SCORE_ONBOARDING: JobSpec(
        lock="user:{user_id}", queueing_lock="score_onboarding:{user_id}"
    ),
    JobName.RECOMPUTE_MEMBER: JobSpec(
        lock="member:{user_id}", queueing_lock="recompute_member:{user_id}"
    ),
    JobName.RECOMPUTE_ALL_MEMBERS: singleton(JobName.RECOMPUTE_ALL_MEMBERS, "0 4 * * *"),
    JobName.REFRESH_RECOMMENDATIONS: JobSpec(
        lock="rec:{project_id}", queueing_lock="refresh_recommendations:{project_id}"
    ),
    JobName.RUN_FORMATION: JobSpec(lock="formation:{event_id}"),
    JobName.MATCH_MENTORS: JobSpec(
        lock="mentor:{request_id}", queueing_lock="match_mentors:{request_id}"
    ),
    JobName.SCAN_COMMUNITY: singleton(JobName.SCAN_COMMUNITY, "0 2 * * *", timeout=1800),
    JobName.COMMUNITY_TICK: singleton(JobName.COMMUNITY_TICK, "*/15 * * * *"),
    JobName.EVENT_TICK: singleton(JobName.EVENT_TICK, "*/5 * * * *"),
    JobName.ADMIN_DIGEST: singleton(JobName.ADMIN_DIGEST, "0 9 * * 1"),
    JobName.ARCHIVE_OLD_EVENTS: singleton(JobName.ARCHIVE_OLD_EVENTS, "30 1 * * *"),
    JobName.CLEANUP: singleton(JobName.CLEANUP, "0 1 * * *"),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Runtime:
    config: Config
    sessionmaker: async_sessionmaker[AsyncSession]
    jobs: procrastinate.App
    algorithm: Algorithm
    clock: Clock
    log: logging.Logger
    http: httpx.AsyncClient
    storage: ObjectStore | None

    async def algo_context(self, session: AsyncSession) -> AlgoContext:

        return AlgoContext(
            now=self.clock.now(),
            settings=await settings.load_settings(session),
            llm=self.config.llm(),
            cache=DatabaseAlgoCache(self.sessionmaker, self.clock),
            log=self.log,
        )


type JobBody = Callable[Concatenate[Runtime, ...], Awaitable[None]]

REGISTRY: dict[JobName, JobBody] = {}


def job(name: JobName) -> Callable[[JobBody], JobBody]:
    def decorate(body: JobBody) -> JobBody:
        if name in REGISTRY:
            raise RuntimeError(f"Job {name} is already registered")
        REGISTRY[name] = body
        return body

    return decorate


def runtime_of(context: JobContext) -> Runtime:
    return context.additional_context["runtime"]


def bind(app: procrastinate.App, name: JobName, body: JobBody) -> None:
    spec = JOBS[name]

    async def run(context: JobContext, **kwargs: Any) -> None:
        kwargs.pop("timestamp", None)
        async with asyncio.timeout(spec.timeout):
            await body(runtime_of(context), **kwargs)

    static = "{" not in f"{spec.lock or ''}{spec.queueing_lock or ''}"
    task = app.task(
        name=name.value,
        pass_context=True,
        retry=RETRY_POLICY,
        lock=spec.lock if static else None,
        queueing_lock=spec.queueing_lock if static else None,
    )(run)
    if spec.cron is not None:
        app.periodic(cron=spec.cron, periodic_id=name.value)(cast(Any, task))


def register_jobs(app: procrastinate.App) -> procrastinate.App:
    for module in JOB_MODULES:
        importlib.import_module(module)
    for name, body in REGISTRY.items():
        bind(app, name, body)
    return app


def create_jobs_app(database_url: str) -> procrastinate.App:
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=conninfo,
            kwargs={"options": "-c search_path=procrastinate,public"},
        ),
    )


def job_locks(name: JobName, kwargs: dict[str, JsonValue]) -> tuple[str | None, str | None]:
    spec = JOBS[name]
    values = {key: "" if value is None else value for key, value in kwargs.items()}
    lock = spec.lock.format_map(values) if spec.lock else None
    queueing_lock = spec.queueing_lock.format_map(values) if spec.queueing_lock else None
    return lock, queueing_lock


async def defer_job(
    session: AsyncSession,
    app: procrastinate.App,
    name: JobName,
    *,
    schedule_in: float | None = None,
    **kwargs: JsonValue,
) -> int | None:
    lock, queueing_lock = job_locks(name, kwargs)
    await session.flush()
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = cast(AsyncConnection[Any], raw.driver_connection)
    task = app.configure_task(
        name=name.value,
        connection=driver,
        lock=lock,
        queueing_lock=queueing_lock,
        schedule_in={"seconds": schedule_in} if schedule_in is not None else None,
    )
    if queueing_lock is None:
        return await task.defer_async(**kwargs)
    await connection.exec_driver_sql("SAVEPOINT defer_job")
    try:
        job_id = await task.defer_async(**kwargs)
    except AlreadyEnqueued:
        await connection.exec_driver_sql("ROLLBACK TO SAVEPOINT defer_job")
        return None
    await connection.exec_driver_sql("RELEASE SAVEPOINT defer_job")
    return job_id


def get_jobs(request: Request) -> procrastinate.App:
    return request.app.state.jobs


Jobs = Annotated[procrastinate.App, Depends(get_jobs)]
