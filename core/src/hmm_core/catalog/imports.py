import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    EventOrigin,
    EventReviewReason,
    EventRevisionOrigin,
    EventStatus,
    ValidationIssue,
)
from hmm_contracts.imports import (
    EventImport,
    EventImportBatch,
    EventImportResponse,
    EventImportResult,
)
from hmm_contracts.notifications import AlertKind, NotificationChange
from pydantic import JsonValue, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.catalog.changes import (
    _json,
    cancel_event,
    notify_event_changed,
)
from hmm_core.catalog.events import (
    DATE_FIELDS,
    NON_TERMINAL_TEAMS,
    NOTIFY_FIELDS,
    TEAM_FIELDS,
    EventAreaInput,
    EventInput,
    create_event,
    enqueue_lead_fits,
    load_event,
    resolve_areas,
)
from hmm_core.catalog.people import (
    sync_people,
)
from hmm_core.formation import service as formation
from hmm_core.notifications.service import raise_alert
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings


def normalize_title(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", title.casefold())
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    return " ".join(text.split())


def payload_hash(item: EventImport) -> str:
    dump = item.model_dump(mode="json")
    dump.pop("source")
    return hashlib.sha256(
        json.dumps(dump, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def find_duplicate(session: AsyncSession, item: EventImport) -> models.Event | None:
    rows = await session.scalars(
        select(models.Event)
        .where(
            models.Event.status != EventStatus.ARCHIVED.value,
            models.Event.starts_at <= item.ends_at + timedelta(days=3),
            models.Event.ends_at >= item.starts_at - timedelta(days=3),
        )
        .order_by(models.Event.starts_at, models.Event.id)
        .limit(50)
    )
    title = normalize_title(item.title)
    for row in rows:
        if normalize_title(row.title) == title:
            return row
    return None


async def map_import_areas(
    session: AsyncSession, item: EventImport
) -> tuple[list[EventAreaInput], list[str]]:
    seen: set[str] = set()
    inputs = []
    for area_input in item.areas:
        if area_input.key in seen:
            continue
        seen.add(area_input.key)
        inputs.append(area_input)
    areas = {
        area.key: area
        for area in await session.scalars(
            select(models.Area).where(models.Area.key.in_(seen), models.Area.archived_at.is_(None))
        )
    }
    mapped: list[EventAreaInput] = []
    unmapped: list[str] = []
    for area_input in inputs:
        area = areas.get(area_input.key)
        if area is None:
            if area_input.key not in unmapped:
                unmapped.append(area_input.key)
            continue
        mapped.append(
            EventAreaInput(
                key=area.key,
                importance=AreaImportance(area_input.importance),
                headcount=area_input.headcount,
            )
        )
    return mapped, unmapped


async def attach_source(
    session: AsyncSession, event: models.Event, item: EventImport, digest: str, now: datetime
) -> models.EventSource:
    source_row = models.EventSource(
        event_id=event.id,
        source_id=item.source.source_id,
        urls=[str(url) for url in item.source.urls],
        payload=item.model_dump(mode="json"),
        payload_hash=digest,
        extractor_version=item.extractor_version,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(source_row)
    await session.flush()
    return source_row


async def touch_source(
    session: AsyncSession,
    source_row: models.EventSource,
    item: EventImport,
    digest: str,
    now: datetime,
) -> None:
    source_row.last_seen_at = now
    if digest != source_row.payload_hash:
        source_row.payload = item.model_dump(mode="json")
        source_row.payload_hash = digest
        source_row.urls = [str(url) for url in item.source.urls]
        source_row.extractor_version = item.extractor_version
    await session.flush()


def import_values(item: EventImport) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "title": item.title,
        "summary": item.summary,
        "description_md": item.description_md,
        "organizer": item.organizer,
        "website_url": str(item.website_url) if item.website_url else None,
        "registration_url": str(item.registration_url) if item.registration_url else None,
        "starts_at": item.starts_at,
        "ends_at": item.ends_at,
        "registration_opens_at": item.registration_opens_at,
        "registration_deadline": item.registration_deadline,
        "timezone": item.timezone or "UTC",
        "format": item.format,
        "location": item.location.model_dump(mode="json", exclude_none=True)
        if item.location
        else None,
        "language": list(item.language),
        "eligibility": item.eligibility,
        "themes": list(item.themes),
        "team_size_min": item.team_size_min,
        "team_size_max": item.team_size_max,
        "prize": item.prize.model_dump(mode="json", exclude_none=True) if item.prize else None,
        "difficulty": Decimal(str(item.difficulty.level)).quantize(Decimal("0.1"), ROUND_HALF_UP)
        if item.difficulty
        else None,
        "difficulty_rationale": item.difficulty.rationale if item.difficulty else None,
        "confidence": Decimal(str(item.confidence)).quantize(Decimal("0.01"), ROUND_HALF_UP),
    }


async def create_imported_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    index: int,
    item: EventImport,
    digest: str,
    now: datetime,
) -> EventImportResult:
    settings = await load_settings(session)
    mapped, unmapped = await map_import_areas(session, item)
    duplicate = await find_duplicate(session, item) is not None
    reasons: set[EventReviewReason] = set()
    if duplicate:
        reasons.add(EventReviewReason.POSSIBLE_DUPLICATE)
    if float(item.confidence) < settings.import_auto_publish_confidence:
        reasons.add(EventReviewReason.LOW_CONFIDENCE)
    if unmapped:
        reasons.add(EventReviewReason.UNMAPPED_AREAS)
    ordered = [reason for reason in EventReviewReason if reason in reasons]
    status = EventStatus.PUBLISHED if not ordered else EventStatus.DRAFT
    values = import_values(item)
    confidence = values.pop("confidence")
    payload = EventInput(**values, areas=mapped)
    event = await create_event(
        session,
        payload,
        origin=EventOrigin.IMPORT,
        status=status,
        created_by=None,
        now=now,
        review_reasons=ordered,
        unmapped_areas=unmapped,
        confidence=confidence,
    )
    await attach_source(session, event, item, digest, now)
    await sync_people(session, jobs, event, item, now)
    if status == EventStatus.PUBLISHED:
        await enqueue_lead_fits(session, jobs, event)
    else:
        await raise_alert(
            session,
            AlertKind.IMPORT_REVIEW,
            now,
            title=f"Event import needs review: {event.title}",
            message=", ".join(reason.value for reason in ordered),
            link=f"/admin/events/{event.id}",
            subject_type="event",
            subject_id=event.id,
            subject_title=event.title,
            dedupe_key=f"import_review:{event.id}",
            extra={"source_id": item.source.source_id},
        )
    return EventImportResult(
        index=index,
        source_id=item.source.source_id,
        result="created" if status == EventStatus.PUBLISHED else "draft",
        event_id=event.id,
        review_reasons=ordered,
        ignored_fields=[],
    )


async def import_event_item(
    session: AsyncSession,
    jobs: procrastinate.App,
    index: int,
    item: EventImport,
    now: datetime,
) -> EventImportResult:
    digest = payload_hash(item)
    event: models.Event | None = None
    source_row = await session.scalar(
        select(models.EventSource)
        .where(models.EventSource.source_id == item.source.source_id)
        .with_for_update()
    )
    previous_hash = source_row.payload_hash if source_row is not None else None
    if source_row is not None:
        event = await load_event(session, source_row.event_id, for_update=True)
    elif item.match_hint is not None and (
        await session.get(models.Event, item.match_hint.event_id) is not None
    ):
        event = await load_event(session, item.match_hint.event_id, for_update=True)
        source_row = await attach_source(session, event, item, digest, now)
    if event is not None and event.status == EventStatus.ARCHIVED.value:
        return EventImportResult(
            index=index,
            source_id=item.source.source_id,
            result="rejected",
            errors=[ValidationIssue(loc=["status"], msg="Event is archived")],
        )
    if event is None or source_row is None:
        return await create_imported_event(session, jobs, index, item, digest, now)
    if item.status == "cancelled":
        return await cancel_imported_event(
            session, jobs, index, item, event, source_row, digest, now
        )
    return await update_imported_event(
        session,
        jobs,
        index,
        item,
        event,
        source_row,
        digest,
        now,
        payload_changed=previous_hash != digest,
    )


def _utc(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return value


async def current_areas(session: AsyncSession, event_id: UUID) -> list[tuple[str, str, int | None]]:
    rows = await session.execute(
        select(models.Area.key, models.EventArea.importance, models.EventArea.headcount)
        .join(models.Area, models.Area.id == models.EventArea.area_id)
        .where(models.EventArea.event_id == event_id)
    )
    return sorted((key, importance, headcount) for key, importance, headcount in rows)


async def team_size_conflict_count(session: AsyncSession, event: models.Event) -> int:
    if event.team_size_min is None and event.team_size_max is None:
        return 0
    ids = list(
        await session.scalars(
            select(models.Project.id).where(
                models.Project.event_id == event.id,
                models.Project.team_status.in_(NON_TERMINAL_TEAMS),
            )
        )
    )
    if not ids:
        return 0
    sizes = {
        project_id: size
        for project_id, size in await session.execute(
            select(models.ProjectMember.project_id, func.count())
            .where(
                models.ProjectMember.project_id.in_(ids),
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
            .group_by(models.ProjectMember.project_id)
        )
    }
    conflicts = 0
    for project_id in ids:
        size = sizes.get(project_id, 0)
        if (event.team_size_max is not None and size > event.team_size_max) or (
            event.team_size_min is not None and size < event.team_size_min
        ):
            conflicts += 1
    return conflicts


async def cancel_imported_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    index: int,
    item: EventImport,
    event: models.Event,
    source_row: models.EventSource,
    digest: str,
    now: datetime,
) -> EventImportResult:
    await touch_source(session, source_row, item, digest, now)
    if event.status == EventStatus.CANCELLED.value:
        return EventImportResult(
            index=index,
            source_id=item.source.source_id,
            result="unchanged",
            event_id=event.id,
            review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
            ignored_fields=[],
        )
    event = await cancel_event(
        session,
        jobs,
        event.id,
        reason="Cancelled by the parser",
        actor=None,
        actor_kind=ActorKind.SERVICE,
        now=now,
    )
    return EventImportResult(
        index=index,
        source_id=item.source.source_id,
        result="updated",
        event_id=event.id,
        review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
        ignored_fields=[],
    )


async def update_imported_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    index: int,
    item: EventImport,
    event: models.Event,
    source_row: models.EventSource,
    digest: str,
    now: datetime,
    *,
    payload_changed: bool,
) -> EventImportResult:
    locked = set(
        await session.scalars(
            select(models.EventFieldLock.field).where(models.EventFieldLock.event_id == event.id)
        )
    )
    applied: dict[str, Any] = {}
    ignored: list[str] = []
    for field, value in import_values(item).items():
        if _json(_utc(getattr(event, field))) == _json(_utc(value)):
            continue
        if field != "confidence" and field in locked:
            ignored.append(field)
        else:
            applied[field] = value
    mapped, unmapped = await map_import_areas(session, item)
    current = await current_areas(session, event.id)
    new_area_rows = sorted((area.key, area.importance.value, area.headcount) for area in mapped)
    area_change = False
    if new_area_rows != current:
        if "areas" in locked:
            ignored.append("areas")
        else:
            area_change = True
    unmapped_changed = list(event.unmapped_areas) != list(unmapped)
    old_reasons = list(event.review_reasons)
    if unmapped:
        new_reasons = (
            old_reasons
            if EventReviewReason.UNMAPPED_AREAS.value in old_reasons
            else [*old_reasons, EventReviewReason.UNMAPPED_AREAS.value]
        )
    else:
        new_reasons = [
            reason for reason in old_reasons if reason != EventReviewReason.UNMAPPED_AREAS.value
        ]
    if not applied and not area_change and not unmapped_changed:
        await touch_source(session, source_row, item, digest, now)
        if payload_changed:
            await sync_people(session, jobs, event, item, now)
        return EventImportResult(
            index=index,
            source_id=item.source.source_id,
            result="unchanged",
            event_id=event.id,
            review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
            ignored_fields=ignored,
        )
    previous_dates = formation.event_dates(event)
    before = {field: getattr(event, field) for field in applied}
    areas_before = current
    unmapped_before = list(event.unmapped_areas)
    for field, value in applied.items():
        setattr(event, field, value.value if isinstance(value, StrEnum) else value)
    if unmapped_changed:
        event.unmapped_areas = list(unmapped)
        event.review_reasons = new_reasons
    if area_change:
        rows = await resolve_areas(session, mapped)
        await session.execute(delete(models.EventArea).where(models.EventArea.event_id == event.id))
        session.add_all(
            models.EventArea(
                event_id=event.id,
                area_id=area.id,
                importance=area_input.importance.value,
                headcount=area_input.headcount,
            )
            for area, area_input in rows
        )
        await session.flush()
    diff: dict[str, JsonValue] = {
        field: {"before": _json(before[field]), "after": _json(getattr(event, field))}
        for field in applied
    }
    if area_change:
        diff["areas"] = {"before": _json(areas_before), "after": _json(new_area_rows)}
    if unmapped_changed:
        diff["unmapped_areas"] = {"before": _json(unmapped_before), "after": _json(unmapped)}
    session.add(
        models.EventRevision(
            event_id=event.id,
            origin=EventRevisionOrigin.IMPORT.value,
            actor_id=None,
            diff=diff,
            ignored_fields=ignored,
        )
    )
    changed = set(applied) | ({"areas"} if area_change else set())
    fields = sorted(changed & NOTIFY_FIELDS)
    if fields:
        await notify_event_changed(
            session,
            event,
            [
                NotificationChange(
                    field=field,
                    previous=None if before[field] is None else str(_json(before[field])),
                    current=None
                    if getattr(event, field) is None
                    else str(_json(getattr(event, field))),
                )
                for field in fields
            ],
            now,
        )
    if changed & DATE_FIELDS:
        await formation.dates_changed(session, jobs, event, previous_dates, now)
    if area_change:
        await enqueue_lead_fits(session, jobs, event)
    if changed & TEAM_FIELDS:
        conflicts = await team_size_conflict_count(session, event)
        if conflicts:
            await raise_alert(
                session,
                AlertKind.TEAM_SIZE_CONFLICT,
                now,
                title=f"Team size conflict: {event.title}",
                message=f"{conflicts} teams are outside the new team size",
                link=f"/admin/events/{event.id}",
                subject_type="event",
                subject_id=event.id,
                subject_title=event.title,
                dedupe_key=f"team_size_conflict:{event.id}",
            )
    await touch_source(session, source_row, item, digest, now)
    if payload_changed:
        await sync_people(session, jobs, event, item, now)
    return EventImportResult(
        index=index,
        source_id=item.source.source_id,
        result="updated",
        event_id=event.id,
        review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
        ignored_fields=ignored,
    )


def raw_source_id(raw: JsonValue) -> str | None:
    if isinstance(raw, dict):
        source = raw.get("source")
        if isinstance(source, dict):
            source_id = source.get("source_id")
            if isinstance(source_id, str):
                return source_id
    return None


async def import_events(
    session: AsyncSession, jobs: procrastinate.App, batch: EventImportBatch, now: datetime
) -> EventImportResponse:
    results: list[EventImportResult] = []
    for index, raw in enumerate(batch.items):
        try:
            item = EventImport.model_validate(raw)
        except ValidationError as exc:
            results.append(
                EventImportResult(
                    index=index,
                    source_id=raw_source_id(raw),
                    result="rejected",
                    errors=[
                        ValidationIssue(loc=list(error["loc"]), msg=error["msg"])
                        for error in exc.errors()
                    ],
                )
            )
            continue
        try:
            async with session.begin_nested():
                result = await import_event_item(session, jobs, index, item, now)
        except ProblemError as error:
            result = EventImportResult(
                index=index,
                source_id=item.source.source_id,
                result="rejected",
                errors=[ValidationIssue(loc=["item"], msg=error.detail or error.code.value)],
            )
        except SQLAlchemyError:
            result = EventImportResult(
                index=index,
                source_id=item.source.source_id,
                result="rejected",
                errors=[ValidationIssue(loc=["item"], msg="Import failed")],
            )
        results.append(result)
    return EventImportResponse(results=results)
