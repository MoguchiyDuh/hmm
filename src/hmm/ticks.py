from collections.abc import Awaitable, Callable
from datetime import datetime

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.attendance.service import expire_going_attendances
from hmm.catalog.parser import check_parser_down
from hmm.feed.digests import send_pending_attendance_digests
from hmm.formation.tick import tick_event
from hmm.mentorship.requests import expire_help_requests
from hmm.mentorship.timers import run_mentorship_timers
from hmm.platform.jobs import JobName, Runtime, job
from hmm.projects.idle import run_project_timers
from hmm.results.timers import run_result_timers
from hmm.results.windows import run_review_timers

type Sweep = Callable[[AsyncSession, procrastinate.App, datetime], Awaitable[object]]

EVENT_SWEEPS: tuple[Sweep, ...] = (
    run_result_timers,
    lambda session, jobs, now: run_review_timers(session, now),
)

COMMUNITY_SWEEPS: tuple[Sweep, ...] = (
    lambda session, jobs, now: expire_help_requests(session, now),
    run_mentorship_timers,
    lambda session, jobs, now: run_project_timers(session, now),
    lambda session, jobs, now: send_pending_attendance_digests(session, now),
    lambda session, jobs, now: expire_going_attendances(session, now),
    lambda session, jobs, now: check_parser_down(session, now),
)


async def sweep(runtime: Runtime, sweeps: tuple[Sweep, ...]) -> None:
    for step in sweeps:
        async with runtime.sessionmaker.begin() as session:
            await step(session, runtime.jobs, runtime.clock.now())


@job(JobName.EVENT_TICK)
async def event_tick(runtime: Runtime, limit: int = 100) -> None:
    async with runtime.sessionmaker.begin() as session:
        query = (
            select(models.Event.id)
            .join(models.EventFormation, models.EventFormation.event_id == models.Event.id)
            .where(
                models.Event.status == "published",
                models.EventFormation.phase.in_(["open", "formed"]),
            )
        )
        event_ids = list(
            await session.scalars(
                query.order_by(models.EventFormation.updated_at, models.Event.id).limit(limit)
            )
        )
    for event_id in event_ids:
        async with runtime.sessionmaker.begin() as session:
            await tick_event(session, runtime, event_id, runtime.clock.now())
    await sweep(runtime, EVENT_SWEEPS)


@job(JobName.COMMUNITY_TICK)
async def community_tick(runtime: Runtime) -> None:
    await sweep(runtime, COMMUNITY_SWEEPS)
