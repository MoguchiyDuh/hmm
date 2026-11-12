import asyncio
from typing import Any, cast

import procrastinate
from procrastinate.job_context import JobContext
from procrastinate.middleware import AsyncCallNext
from procrastinate.worker import Worker
from psycopg import AsyncConnection
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.api.schemas.admin import JobName
from hmm_core.config import Config

__all__ = ["RETRY_POLICY", "JobName", "create_jobs_app", "defer_job", "main"]

RETRY_POLICY = procrastinate.RetryStrategy(max_attempts=5, exponential_wait=2)


async def failure_alert(call_next: AsyncCallNext, context: JobContext, worker: Worker) -> Any:
    try:
        return await call_next()
    except Exception as error:
        strategy = context.task.retry_strategy
        if (
            strategy is None
            or strategy.get_retry_decision(exception=error, job=context.job) is None
        ):
            await context.app.connector.execute_query_async(
                query="""
                INSERT INTO public.admin_alerts
                    (type, severity, payload, dedupe_key, status)
                VALUES (
                    'job_failing', 'critical', %(payload)s,
                    'job_failing:' || %(task_name)s || ':' ||
                        to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD-HH24'),
                    'open'
                )
                ON CONFLICT (dedupe_key) WHERE status = 'open' DO NOTHING
                """,
                task_name=context.job.task_name,
                payload={
                    "task_name": context.job.task_name,
                    "job_id": context.job.id,
                    "error": str(error),
                },
            )
        raise


def create_jobs_app(database_url: str) -> procrastinate.App:
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=conninfo,
            kwargs={"options": "-c search_path=procrastinate,public"},
        ),
        worker_defaults={"worker_middleware": [failure_alert]},
    )


async def defer_job(
    session: AsyncSession,
    app: procrastinate.App,
    name: JobName,
    **kwargs: JsonValue,
) -> int:
    await session.flush()
    await session.execute(text("SET LOCAL search_path TO procrastinate, public"))
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = cast(AsyncConnection[Any], raw.driver_connection)
    return await app.configure_task(name=name.value, connection=driver).defer_async(**kwargs)


async def run() -> None:
    config = Config.from_env()
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required to run the worker")
    app = create_jobs_app(config.database_url)
    async with app.open_async():
        await app.run_worker_async()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
