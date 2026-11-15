import asyncio
import logging
from typing import Any

import httpx
from procrastinate.job_context import JobContext
from procrastinate.middleware import AsyncCallNext, WorkerMiddleware
from procrastinate.worker import Worker
from sqlalchemy import update

from hmm import models
from hmm.algo import get_algorithm
from hmm.common import AlertStatus
from hmm.notifications.payloads import AlertKind
from hmm.notifications.service import raise_alert
from hmm.platform.clock import SystemClock
from hmm.platform.config import Config
from hmm.platform.db import create_engine, create_sessionmaker
from hmm.platform.jobs import Runtime, create_jobs_app, register_jobs, runtime_of
from hmm.platform.logs import configure_logging, log_fields
from hmm.platform.storage import create_storage


def final_failure(context: JobContext, error: Exception) -> bool:
    strategy = context.task.retry_strategy
    return strategy is None or strategy.get_retry_decision(exception=error, job=context.job) is None


async def failure_alert(call_next: AsyncCallNext, context: JobContext, worker: Worker) -> Any:
    runtime = runtime_of(context)
    task = context.job.task_name
    token = log_fields.set({"job_id": context.job.id or 0, "task": task})
    try:
        result = await call_next()
    except Exception as error:
        if final_failure(context, error):
            now = runtime.clock.now()
            async with runtime.sessionmaker.begin() as session:
                await raise_alert(
                    session,
                    AlertKind.JOB_FAILING,
                    now,
                    title=f"Job {task} failed",
                    message=str(error),
                    link="/admin/system",
                    subject_type="job",
                    subject_title=task,
                    dedupe_key=f"job_failing:{task}:{now:%Y-%m-%d-%H}",
                    extra={"task_name": task, "job_id": context.job.id, "error": str(error)},
                )
        raise
    else:
        async with runtime.sessionmaker.begin() as session:
            await session.execute(
                update(models.AdminAlert)
                .where(
                    models.AdminAlert.type == AlertKind.JOB_FAILING.value,
                    models.AdminAlert.status == AlertStatus.OPEN.value,
                    models.AdminAlert.payload["task_name"].astext == task,
                )
                .values(status=AlertStatus.RESOLVED.value, resolved_at=runtime.clock.now())
            )
        return result
    finally:
        log_fields.reset(token)


WORKER_MIDDLEWARE: list[WorkerMiddleware] = [failure_alert]


async def run(config: Config) -> None:
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required to run the worker")
    app = register_jobs(create_jobs_app(config.database_url))
    engine = create_engine(config.database_url)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            runtime = Runtime(
                config=config,
                sessionmaker=create_sessionmaker(engine),
                jobs=app,
                algorithm=get_algorithm(),
                clock=SystemClock(),
                log=logging.getLogger("hmm.jobs"),
                http=http,
                storage=create_storage(config),
            )
            async with app.open_async():
                await app.run_worker_async(
                    concurrency=4,
                    additional_context={"runtime": runtime},
                    worker_middleware=WORKER_MIDDLEWARE,
                )
    finally:
        await engine.dispose()


def main() -> None:
    config = Config.from_env()
    configure_logging(config.log_level)
    asyncio.run(run(config))
