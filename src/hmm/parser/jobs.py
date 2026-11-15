import asyncio
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.areas.service import list_areas
from hmm.catalog.import_schema import (
    EventImportBatch,
    ParserHeartbeat,
    ParserStats,
    TrackedEvent,
)
from hmm.catalog.imports import import_events
from hmm.catalog.parser import record_heartbeat, tracked_events
from hmm.parser.config import SourceConfig, load_sources
from hmm.parser.fetch import Fetcher, Resolver, Sleeper, resolve_host
from hmm.parser.pipeline import SourceRun, chunks, collect, dedupe
from hmm.platform.jobs import JobName, Runtime, job
from hmm.platform.pagination import CursorParams

BATCH_SIZE = 50
TRACKED_PAGE = 100
DUE_SLACK = timedelta(minutes=5)
REVISIT_UPCOMING = timedelta(days=1)
REVISIT_ENDED = timedelta(days=7)


def is_due(source: SourceConfig, state: models.ParserSourceState | None, now: datetime) -> bool:
    if state is None:
        return True
    return now - state.last_started_at >= source.interval - DUE_SLACK


def revisit_due(event: TrackedEvent, checked_at: datetime | None, now: datetime) -> bool:
    if checked_at is None:
        return True
    if event.ends_at > now:
        return now - checked_at >= REVISIT_UPCOMING - DUE_SLACK
    if checked_at < event.ends_at:
        return True
    return now - checked_at >= REVISIT_ENDED - DUE_SLACK


async def all_tracked(session: AsyncSession, now: datetime) -> list[TrackedEvent]:
    events: list[TrackedEvent] = []
    cursor: str | None = None
    while True:
        page = await tracked_events(session, CursorParams(cursor=cursor, limit=TRACKED_PAGE), now)
        events.extend(page.items)
        if page.next_cursor is None:
            return events
        cursor = page.next_cursor


def usable(
    sources: list[SourceConfig], env: Mapping[str, str], runtime: Runtime
) -> list[SourceConfig]:
    ready: list[SourceConfig] = []
    for source in sources:
        if not source.enabled:
            continue
        missing = source.missing_secrets(env)
        if missing:
            runtime.log.info("parser %s disabled: missing %s", source.id, ", ".join(missing))
            continue
        ready.append(source)
    return ready


async def run_parser(
    runtime: Runtime,
    *,
    env: Mapping[str, str] = os.environ,
    resolve: Resolver = resolve_host,
    sleep: Sleeper = asyncio.sleep,
) -> ParserHeartbeat:
    started_at = runtime.clock.now()
    path = Path(runtime.config.parser_sources) if runtime.config.parser_sources else None
    sources = usable(load_sources(path), env, runtime)
    async with runtime.sessionmaker.begin() as session:
        areas = frozenset(area.key for area in await list_areas(session, include_archived=False))
        tracked = await all_tracked(session, started_at)
        checked = {
            row.source_id: row.checked_at
            for row in await session.scalars(select(models.ParserRevisit))
        }
        states = {
            state.source_id: state
            for state in await session.scalars(select(models.ParserSourceState).with_for_update())
        }
        due = [source for source in sources if is_due(source, states.get(source.id), started_at)]
        for source in due:
            state = states.get(source.id)
            if state is None:
                session.add(
                    models.ParserSourceState(
                        source_id=source.id,
                        adapter=source.adapter,
                        last_started_at=started_at,
                        stats={},
                    )
                )
            else:
                state.adapter = source.adapter
                state.last_started_at = started_at
    fetcher = Fetcher(client=runtime.http, resolve=resolve, sleep=sleep)
    revisits: dict[str, list[TrackedEvent]] = {}
    for event in tracked:
        if revisit_due(event, checked.get(event.source_id), started_at):
            revisits.setdefault(event.source_id.split(":", 1)[0], []).append(event)
    runs = await asyncio.gather(
        *(
            collect(
                source,
                fetcher=fetcher,
                now=started_at,
                areas=areas,
                env=env,
                log=runtime.log,
                revisits=revisits.get(source.id, ()),
            )
            for source in due
        )
    )
    known = {event.source_id for event in tracked}
    items, merged = dedupe(runs)
    items = [item for item in items if item.status != "cancelled" or item.source.source_id in known]
    errors: list[JsonValue] = [error for run in runs for error in run.errors]
    imported = 0
    rejected: dict[str, int] = {}
    for batch in chunks(items, BATCH_SIZE):
        async with runtime.sessionmaker.begin() as session:
            response = await import_events(
                session, runtime.jobs, EventImportBatch.of(batch), runtime.clock.now()
            )
        for result in response.results:
            if result.result == "rejected":
                prefix = (result.source_id or "?").split(":", 1)[0]
                rejected[prefix] = rejected.get(prefix, 0) + 1
                errors.append(
                    {
                        "source": prefix,
                        "stage": "import",
                        "candidate": result.source_id,
                        "error": "; ".join(issue.msg for issue in result.errors)[:500],
                    }
                )
            else:
                imported += 1
    finished_at = runtime.clock.now()
    heartbeat = ParserHeartbeat(
        run_id=f"parser-{started_at:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}",
        started_at=started_at,
        finished_at=finished_at,
        stats=ParserStats(
            sources_checked=len(runs),
            candidates=sum(run.candidates for run in runs),
            imported=imported,
            errors=len(errors),
        ),
        errors=errors[:200],
    )
    async with runtime.sessionmaker.begin() as session:
        await store_states(session, runs, rejected, finished_at)
        await store_revisits(session, runs, known, finished_at)
        await record_heartbeat(session, heartbeat)
    runtime.log.info(
        "parser run: %d sources, %d items, %d merged, %d imported, %d errors",
        len(runs),
        len(items),
        merged,
        imported,
        len(errors),
    )
    return heartbeat


async def store_states(
    session: AsyncSession,
    runs: list[SourceRun],
    rejected: Mapping[str, int],
    finished_at: datetime,
) -> None:
    for run in runs:
        state = await session.get(models.ParserSourceState, run.source.id, with_for_update=True)
        if state is None:
            continue
        state.last_finished_at = finished_at
        failed = any(error.get("stage") in {"discover", "budget"} for error in run.errors)
        if failed:
            state.last_error = str(run.errors[-1].get("error"))
        else:
            state.last_success_at = finished_at
            state.last_error = None
        state.stats = {**run.stats(), "rejected": rejected.get(run.source.id, 0)}


async def store_revisits(
    session: AsyncSession, runs: list[SourceRun], known: set[str], checked_at: datetime
) -> None:
    for run in runs:
        for source_id, found in run.checks.items():
            row = await session.get(models.ParserRevisit, source_id, with_for_update=True)
            if row is None:
                session.add(
                    models.ParserRevisit(
                        source_id=source_id, checked_at=checked_at, misses=0 if found else 1
                    )
                )
            else:
                row.checked_at = checked_at
                row.misses = 0 if found else row.misses + 1
    await session.execute(
        delete(models.ParserRevisit).where(models.ParserRevisit.source_id.not_in(known))
    )


@job(JobName.PARSER_RUN)
async def parser_run(runtime: Runtime) -> None:
    await run_parser(runtime)
