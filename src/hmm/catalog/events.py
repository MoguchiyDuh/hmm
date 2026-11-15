import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Self
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import EventArea, EventSnapshot
from hmm.catalog import schemas
from hmm.common import (
    ActorKind,
    AreaImportance,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventReviewReason,
    EventStatus,
)
from hmm.formation import common as formation_common
from hmm.platform.audit import record_audit
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.scores import snapshot as scores_snapshot

SLUG_MAX = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class EventAreaInput:
    key: str
    importance: AreaImportance
    headcount: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EventInput:
    kind: EventKind
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    slug: str | None = None
    formation_enabled: bool | None = None
    summary: str | None = None
    description_md: str | None = None
    organizer: str | None = None
    website_url: str | None = None
    registration_url: str | None = None
    registration_opens_at: datetime | None = None
    registration_deadline: datetime | None = None
    format: EventFormat | None = None
    location: dict[str, JsonValue] | None = None
    language: Sequence[str] = ()
    eligibility: str | None = None
    themes: Sequence[str] = ()
    areas: Sequence[EventAreaInput] = ()
    team_size_min: int | None = None
    team_size_max: int | None = None
    prize: dict[str, JsonValue] | None = None
    difficulty: Decimal | None = None
    difficulty_rationale: str | None = None

    @classmethod
    def from_create(cls, body: schemas.CreateEventBody) -> Self:
        return cls(
            kind=body.kind,
            title=body.title,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            timezone=body.timezone,
            slug=body.slug,
            formation_enabled=body.formation_enabled,
            summary=body.summary,
            description_md=body.description_md,
            organizer=body.organizer,
            website_url=str(body.website_url) if body.website_url is not None else None,
            registration_url=str(body.registration_url)
            if body.registration_url is not None
            else None,
            registration_opens_at=body.registration_opens_at,
            registration_deadline=body.registration_deadline,
            format=body.format,
            location=(body.location.model_dump(mode="json", exclude_none=True) or None)
            if body.location is not None
            else None,
            language=body.language,
            eligibility=body.eligibility,
            themes=body.themes,
            areas=[
                EventAreaInput(key=area.key, importance=area.importance, headcount=area.headcount)
                for area in body.areas
            ],
            team_size_min=body.team_size_min,
            team_size_max=body.team_size_max,
            prize=(body.prize.model_dump(mode="json", exclude_none=True) or None)
            if body.prize is not None
            else None,
            difficulty=Decimal(str(body.difficulty)).quantize(Decimal("0.1"), ROUND_HALF_UP)
            if body.difficulty is not None
            else None,
            difficulty_rationale=body.difficulty_rationale,
        )


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:SLUG_MAX].strip("-") or "event"


async def unique_slug(session: AsyncSession, base: str) -> str:
    candidate = base
    for attempt in range(1, 51):
        if attempt > 1:
            suffix = f"-{attempt}"
            candidate = base[: SLUG_MAX - len(suffix)].rstrip("-") + suffix
        if not await session.scalar(select(exists().where(models.Event.slug == candidate))):
            return candidate
    raise ProblemError(ErrorCode.INVALID_STATE, "Could not allocate an event slug")


def default_formation_enabled(kind: EventKind, team_size_max: int | None) -> bool:
    return kind in {EventKind.HACKATHON, EventKind.CONTEST} and (
        team_size_max is None or team_size_max >= 2
    )


async def resolve_areas(
    session: AsyncSession, inputs: Sequence[EventAreaInput]
) -> list[tuple[models.Area, EventAreaInput]]:
    keys: set[str] = set()
    for area in inputs:
        if area.key in keys:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Duplicate area: {area.key}")
        keys.add(area.key)
    rows = await session.scalars(
        select(models.Area).where(models.Area.key.in_(keys), models.Area.archived_at.is_(None))
    )
    areas = {area.key: area for area in rows}
    resolved: list[tuple[models.Area, EventAreaInput]] = []
    for area in inputs:
        if area.key not in areas:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Unknown area: {area.key}")
        resolved.append((areas[area.key], area))
    return resolved


async def create_event(
    session: AsyncSession,
    payload: EventInput,
    *,
    origin: EventOrigin,
    status: EventStatus,
    created_by: UUID | None,
    now: datetime,
    review_reasons: Sequence[EventReviewReason] = (),
    unmapped_areas: Sequence[str] = (),
    confidence: Decimal | None = None,
) -> models.Event:
    if (
        payload.formation_enabled is True
        and payload.team_size_max is not None
        and payload.team_size_max < 2
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation needs team_size_max of at least 2")
    formation_enabled = (
        payload.formation_enabled
        if payload.formation_enabled is not None
        else default_formation_enabled(payload.kind, payload.team_size_max)
    )
    if payload.slug is not None:
        if await session.scalar(select(exists().where(models.Event.slug == payload.slug))):
            raise ProblemError(ErrorCode.VALIDATION_ERROR, "Event slug already exists")
        slug = payload.slug
    else:
        slug = await unique_slug(session, slugify(payload.title))
    areas = await resolve_areas(session, payload.areas)
    event = models.Event(
        slug=slug,
        kind=payload.kind.value,
        formation_enabled=formation_enabled,
        title=payload.title,
        summary=payload.summary,
        description_md=payload.description_md,
        organizer=payload.organizer,
        website_url=payload.website_url,
        registration_url=payload.registration_url,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        registration_opens_at=payload.registration_opens_at,
        registration_deadline=payload.registration_deadline,
        timezone=payload.timezone,
        format=payload.format.value if payload.format is not None else None,
        location=payload.location,
        language=list(payload.language),
        eligibility=payload.eligibility,
        themes=list(payload.themes),
        team_size_min=payload.team_size_min,
        team_size_max=payload.team_size_max,
        prize=payload.prize,
        difficulty=payload.difficulty.quantize(Decimal("0.1"), ROUND_HALF_UP)
        if payload.difficulty is not None
        else None,
        difficulty_rationale=payload.difficulty_rationale,
        status=status.value,
        review_reasons=[EventReviewReason(reason).value for reason in review_reasons],
        origin=origin.value,
        created_by=created_by,
        confidence=confidence.quantize(Decimal("0.01")) if confidence is not None else None,
        unmapped_areas=list(unmapped_areas),
    )
    session.add(event)
    await session.flush()
    session.add_all(
        models.EventArea(
            event_id=event.id,
            area_id=area.id,
            importance=area_input.importance.value,
            headcount=area_input.headcount,
        )
        for area, area_input in areas
    )
    await session.flush()
    if formation_enabled and status == EventStatus.PUBLISHED:
        await formation_common.ensure_formation(session, event, now)
    return event


async def event_snapshot(session: AsyncSession, event_id: UUID) -> EventSnapshot:
    event = await get_or_404(session, models.Event, event_id)
    rows = await session.execute(
        select(
            models.Area.key,
            models.Area.name,
            models.EventArea.importance,
            models.EventArea.headcount,
        )
        .join(models.Area, models.Area.id == models.EventArea.area_id)
        .where(models.EventArea.event_id == event_id)
        .order_by(models.Area.sort, models.Area.key)
    )
    return scores_snapshot.event_snapshot(
        event,
        [
            EventArea(area=key, importance=AreaImportance(importance), headcount=headcount)
            for key, _, importance, headcount in rows
        ],
    )


async def create_manual_event(
    session: AsyncSession,
    body: schemas.CreateEventBody,
    actor: models.User,
    now: datetime,
) -> models.Event:
    payload = EventInput.from_create(body)
    event = await create_event(
        session,
        payload,
        origin=EventOrigin.MANUAL,
        status=EventStatus.PUBLISHED,
        created_by=actor.id,
        now=now,
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_CREATE,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before=None,
        after={
            "slug": event.slug,
            "kind": event.kind,
            "title": event.title,
            "starts_at": event.starts_at.isoformat(),
            "ends_at": event.ends_at.isoformat(),
            "formation_enabled": event.formation_enabled,
        },
        reason=body.reason,
    )
    return event
