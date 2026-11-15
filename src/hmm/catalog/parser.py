from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.catalog.import_schema import ParserHeartbeat as ParserHeartbeatBody
from hmm.catalog.import_schema import TrackedEvent, TrackedEventsResponse
from hmm.common import EventStatus
from hmm.notifications.payloads import AlertKind
from hmm.notifications.service import raise_alert, resolve_alerts
from hmm.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm.platform.settings import load_settings


async def tracked_events(
    session: AsyncSession, pagination: CursorParams, now: datetime
) -> TrackedEventsResponse:
    cutoff = now - timedelta(days=30)
    statement = apply_keyset(
        select(models.EventSource)
        .join(models.Event, models.Event.id == models.EventSource.event_id)
        .where(models.Event.status != EventStatus.ARCHIVED.value, models.Event.ends_at >= cutoff),
        pagination,
        sort=[models.Event.ends_at],
        id_column=models.EventSource.id,
        descending=False,
    ).add_columns(models.Event.ends_at)
    rows = list((await session.execute(statement)).all())
    items = rows[: pagination.limit]
    if not items:
        return TrackedEventsResponse(items=[], next_cursor=None)
    next_cursor = (
        encode_cursor([items[-1][1], items[-1][0].id]) if len(rows) > pagination.limit else None
    )
    return TrackedEventsResponse(
        items=[
            TrackedEvent(source_id=source.source_id, urls=list(source.urls), ends_at=ends_at)
            for source, ends_at in items
        ],
        next_cursor=next_cursor,
    )


async def record_heartbeat(session: AsyncSession, body: ParserHeartbeatBody) -> None:
    row = await session.scalar(
        select(models.ParserHeartbeat)
        .where(models.ParserHeartbeat.run_id == body.run_id)
        .with_for_update()
    )
    if row is None:
        session.add(
            models.ParserHeartbeat(
                run_id=body.run_id,
                started_at=body.started_at,
                finished_at=body.finished_at,
                stats=body.stats.model_dump(mode="json"),
                errors=list(body.errors),
            )
        )
    else:
        row.started_at = body.started_at
        row.finished_at = body.finished_at
        row.stats = body.stats.model_dump(mode="json")
        row.errors = list(body.errors)
    await session.flush()


async def check_parser_down(session: AsyncSession, now: datetime) -> None:
    settings = await load_settings(session)
    last = await session.scalar(select(func.max(models.ParserHeartbeat.finished_at)))
    if last is None:
        return
    if now - last > timedelta(hours=settings.parser_heartbeat_max_age_hours):
        await raise_alert(
            session,
            AlertKind.PARSER_DOWN,
            now,
            title="Parser is down",
            message=f"No parser heartbeat since {last.isoformat()}",
            link="/admin/system",
            subject_type="parser",
            subject_title="Parser",
            dedupe_key="parser_down",
        )
    else:
        await resolve_alerts(session, "parser_down", now)
