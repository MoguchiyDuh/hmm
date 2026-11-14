import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum, StrEnum
from hashlib import sha1
from typing import Any, Self
from uuid import UUID

import procrastinate
from hmm_contracts.algo import EventArea, EventSnapshot
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    ConfirmationFactType,
    ConfirmationSource,
    EnrollmentStatus,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventPhase,
    EventReviewReason,
    EventRevisionOrigin,
    EventStatus,
    FormationPhase,
    InviteStatus,
    JoinRequestStatus,
    LeadOfferStatus,
    MergeProposalStatus,
    PlaceStatus,
    ProjectStatus,
    ResultOutcome,
    SourcePersonRole,
    SwapRequestStatus,
    TeamStatus,
    Trust,
    ValidationIssue,
)
from hmm_contracts.imports import (
    EventImport,
    EventImportBatch,
    EventImportResponse,
    EventImportResult,
    Location,
    Prize,
    SourcePerson,
)
from hmm_contracts.notifications import (
    AlertKind,
    EventCancelledPayload,
    EventChangedPayload,
    NotificationChange,
)
from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance.service import attendance_view
from hmm_core.catalog import schemas
from hmm_core.feed import service as feed
from hmm_core.formation import service as formation
from hmm_core.notifications.service import notify, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import EventAction, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import service as projects
from hmm_core.projects.schemas import ProjectView

SLUG_MAX = 100
LIVE_ENROLLMENTS = frozenset(
    {EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE}
)
TEAM_TERMINAL = frozenset({TeamStatus.DISBANDED, TeamStatus.CANCELLED})
LOCKABLE_SKIP = frozenset({"status", "review_reasons"})
NOTIFY_FIELDS = frozenset(
    {
        "starts_at",
        "ends_at",
        "team_size_min",
        "team_size_max",
        "format",
        "registration_deadline",
    }
)
DATE_FIELDS = frozenset({"starts_at", "ends_at", "registration_deadline"})
TEAM_FIELDS = frozenset({"team_size_min", "team_size_max"})
NON_TERMINAL_TEAMS = (
    TeamStatus.FORMING.value,
    TeamStatus.REVIEW.value,
    TeamStatus.LOCKED.value,
    TeamStatus.ACTIVE.value,
)


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


def event_phase(event: models.Event, now: datetime) -> EventPhase:
    if event.registration_opens_at is not None and now < event.registration_opens_at:
        return EventPhase.UPCOMING
    if now < (event.registration_deadline or event.starts_at):
        return EventPhase.REGISTRATION_OPEN
    if now < event.starts_at:
        return EventPhase.REGISTRATION_CLOSED
    if now < event.ends_at:
        return EventPhase.ONGOING
    return EventPhase.ENDED


async def load_event(
    session: AsyncSession, event_id: UUID, *, for_update: bool = False
) -> models.Event:
    statement = select(models.Event).where(models.Event.id == event_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    event = await session.scalar(statement)
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event


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
        await formation.ensure_formation(session, event, now)
    return event


async def enqueue_lead_fits(
    session: AsyncSession, jobs: procrastinate.App, event: models.Event
) -> None:
    if event.formation_enabled and event.status == EventStatus.PUBLISHED:
        await defer_job(session, jobs, JobName.COMPUTE_LEAD_FITS, event_id=str(event.id))


async def event_snapshot(session: AsyncSession, event_id: UUID) -> EventSnapshot:
    event = await load_event(session, event_id)
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
    return EventSnapshot(
        id=event.id,
        title=event.title,
        timezone=event.timezone,
        summary=event.summary,
        description_md=event.description_md,
        themes=event.themes,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        team_size_min=event.team_size_min,
        team_size_max=event.team_size_max,
        created_by=event.created_by,
        areas=[
            EventArea(area=key, importance=AreaImportance(importance), headcount=headcount)
            for key, _, importance, headcount in rows
        ],
        format=EventFormat(event.format) if event.format else None,
        kind=EventKind(event.kind),
        origin=EventOrigin(event.origin),
        difficulty=float(event.difficulty) if event.difficulty is not None else None,
    )


@dataclass(frozen=True, slots=True)
class EventParticipation:
    attendance: models.EventAttendance | None
    enrollment: models.Enrollment | None
    on_team: bool
    pending_join_request: bool
    lead_fit: models.LeadFit | None
    phase: FormationPhase | None
    formation_at: datetime | None


async def participation_states(
    session: AsyncSession, events: Sequence[models.Event], actor: models.User, now: datetime
) -> dict[UUID, EventParticipation]:
    if not events:
        return {}
    ids = [event.id for event in events]
    attendances = {
        attendance.event_id: attendance
        for attendance in await session.scalars(
            select(models.EventAttendance).where(
                models.EventAttendance.event_id.in_(ids),
                models.EventAttendance.user_id == actor.id,
            )
        )
    }
    enrollments: dict[UUID, models.Enrollment] = {}
    for enrollment in await session.scalars(
        select(models.Enrollment)
        .where(models.Enrollment.event_id.in_(ids), models.Enrollment.user_id == actor.id)
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
    ):
        current = enrollments.get(enrollment.event_id)
        if current is None or (
            enrollment.status in LIVE_ENROLLMENTS and current.status not in LIVE_ENROLLMENTS
        ):
            enrollments[enrollment.event_id] = enrollment
    on_team = set(
        await session.scalars(
            select(models.ProjectMember.event_id)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.ProjectMember.event_id.in_(ids),
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
        )
    )
    pending = set(
        await session.scalars(
            select(models.JoinRequest.event_id).where(
                models.JoinRequest.event_id.in_(ids),
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING,
            )
        )
    )
    fits = {
        fit.event_id: fit
        for fit in await session.scalars(
            select(models.LeadFit).where(
                models.LeadFit.event_id.in_(ids), models.LeadFit.user_id == actor.id
            )
        )
    }
    formations = {
        event_id: (FormationPhase(phase), formation_at)
        for event_id, phase, formation_at in await session.execute(
            select(
                models.EventFormation.event_id,
                models.EventFormation.phase,
                models.EventFormation.formation_at,
            ).where(models.EventFormation.event_id.in_(ids))
        )
    }
    return {
        event_id: EventParticipation(
            attendance=attendances.get(event_id),
            enrollment=enrollments.get(event_id),
            on_team=event_id in on_team,
            pending_join_request=event_id in pending,
            lead_fit=fits.get(event_id),
            phase=formations[event_id][0] if event_id in formations else None,
            formation_at=formations[event_id][1] if event_id in formations else None,
        )
        for event_id in ids
    }


def event_actions(
    event: models.Event,
    state: EventParticipation,
    actor: models.User,
    now: datetime,
    settings: RuntimeSettings,
) -> list[EventAction]:
    published = event.status == EventStatus.PUBLISHED
    admin = actor.is_admin
    reg_deadline = event.registration_deadline or event.starts_at
    formation_open = state.phase == FormationPhase.OPEN
    attended_window = (
        event.starts_at <= now <= event.ends_at + timedelta(days=settings.attendance_claim_days)
    )
    allowed: set[EventAction] = set()
    if admin or (event.created_by == actor.id and now < event.starts_at):
        allowed.add(EventAction.EDIT)
    if admin or (event.created_by == actor.id and published):
        allowed.add(EventAction.CANCEL)
    if (
        published
        and now < event.ends_at
        and (state.attendance is None or state.attendance.status != AttendanceStatus.GOING)
    ):
        allowed.add(EventAction.GOING)
    if (
        published
        and attended_window
        and (state.attendance is None or state.attendance.status != AttendanceStatus.ATTENDED)
    ):
        allowed.add(EventAction.ATTENDED)
    if event.formation_enabled and published and formation_open and not state.on_team:
        if now < reg_deadline and (
            state.enrollment is None or state.enrollment.status not in LIVE_ENROLLMENTS
        ):
            allowed.add(EventAction.ENROLL)
        if (
            now < event.starts_at
            and state.enrollment is not None
            and state.enrollment.status in LIVE_ENROLLMENTS
        ):
            allowed.add(EventAction.WITHDRAW)
        if (
            now < (state.formation_at or reg_deadline)
            and state.enrollment is not None
            and state.enrollment.status == EnrollmentStatus.ACTIVE
            and not state.pending_join_request
        ):
            allowed.add(EventAction.REQUEST_JOIN)
        if (
            now < reg_deadline
            and state.lead_fit is not None
            and float(state.lead_fit.fit) >= settings.lead_fit_threshold
        ):
            allowed.add(EventAction.CLAIM_LEAD)
    return [action for action in EventAction if action in allowed]


async def area_details(
    session: AsyncSession, event_ids: Sequence[UUID]
) -> dict[UUID, list[schemas.EventAreaDetail]]:
    details: dict[UUID, list[schemas.EventAreaDetail]] = {event_id: [] for event_id in event_ids}
    if not event_ids:
        return details
    rows = await session.execute(
        select(
            models.EventArea.event_id,
            models.Area.key,
            models.Area.name,
            models.EventArea.importance,
            models.EventArea.headcount,
        )
        .join(models.Area, models.Area.id == models.EventArea.area_id)
        .where(models.EventArea.event_id.in_(event_ids))
        .order_by(models.Area.sort, models.Area.key)
    )
    for event_id, key, name, importance, headcount in rows:
        details[event_id].append(
            schemas.EventAreaDetail(
                key=key, name=name, importance=AreaImportance(importance), headcount=headcount
            )
        )
    return details


async def attendance_counts(
    session: AsyncSession, event_ids: Sequence[UUID]
) -> dict[UUID, schemas.AttendanceCounts]:
    counts = {event_id: schemas.AttendanceCounts(going=0, attended=0) for event_id in event_ids}
    if not event_ids:
        return counts
    rows = await session.execute(
        select(models.EventAttendance.event_id, models.EventAttendance.status, func.count())
        .where(
            models.EventAttendance.event_id.in_(event_ids),
            models.EventAttendance.status.in_([AttendanceStatus.GOING, AttendanceStatus.ATTENDED]),
        )
        .group_by(models.EventAttendance.event_id, models.EventAttendance.status)
    )
    for event_id, status, count in rows:
        if status == AttendanceStatus.GOING:
            counts[event_id] = schemas.AttendanceCounts(
                going=count, attended=counts[event_id].attended
            )
        else:
            counts[event_id] = schemas.AttendanceCounts(
                going=counts[event_id].going, attended=count
            )
    return counts


async def formation_phases(
    session: AsyncSession, event_ids: Sequence[UUID]
) -> dict[UUID, FormationPhase]:
    if not event_ids:
        return {}
    return {
        event_id: FormationPhase(phase)
        for event_id, phase in await session.execute(
            select(models.EventFormation.event_id, models.EventFormation.phase).where(
                models.EventFormation.event_id.in_(event_ids)
            )
        )
    }


def event_summary(
    event: models.Event,
    *,
    actor: models.User,
    now: datetime,
    settings: RuntimeSettings,
    areas: list[schemas.EventAreaDetail],
    counts: schemas.AttendanceCounts,
    attendance: models.EventAttendance | None,
    participation_phase: FormationPhase | None,
    state: EventParticipation,
) -> schemas.EventSummary:
    return schemas.EventSummary(
        id=event.id,
        slug=event.slug,
        kind=EventKind(event.kind),
        formation_enabled=event.formation_enabled,
        title=event.title,
        summary=event.summary,
        organizer=event.organizer,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        registration_opens_at=event.registration_opens_at,
        registration_deadline=event.registration_deadline,
        timezone=event.timezone,
        format=EventFormat(event.format) if event.format else None,
        location=Location.model_validate(event.location) if event.location else None,
        areas=areas,
        difficulty=float(event.difficulty) if event.difficulty is not None else None,
        status=EventStatus(event.status),
        phase=event_phase(event, now),
        participation_phase=participation_phase,
        origin=EventOrigin(event.origin),
        attendance_counts=counts,
        my_attendance=attendance_view(attendance, event, actor, actor) if attendance else None,
        allowed_actions=event_actions(event, state, actor, now, settings),
    )


async def event_detail(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.EventDetail:
    settings = await load_settings(session)
    state = (await participation_states(session, [event], actor, now))[event.id]
    summary = event_summary(
        event,
        actor=actor,
        now=now,
        settings=settings,
        areas=(await area_details(session, [event.id])).get(event.id, []),
        counts=(await attendance_counts(session, [event.id]))[event.id],
        attendance=state.attendance,
        participation_phase=(await formation_phases(session, [event.id])).get(event.id),
        state=state,
    )
    sources = [
        schemas.EventSource(
            id=source.id,
            source_id=source.source_id,
            urls=list(source.urls),
            extractor_version=source.extractor_version,
            first_seen_at=source.first_seen_at,
            last_seen_at=source.last_seen_at,
        )
        for source in await session.scalars(
            select(models.EventSource)
            .where(models.EventSource.event_id == event.id)
            .order_by(models.EventSource.first_seen_at, models.EventSource.id)
        )
    ]
    members_going = [
        UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url)
        for user in await session.scalars(
            select(models.User)
            .join(models.EventAttendance, models.EventAttendance.user_id == models.User.id)
            .where(
                models.EventAttendance.event_id == event.id,
                models.EventAttendance.status == AttendanceStatus.GOING,
            )
            .order_by(models.EventAttendance.status_at, models.User.id)
        )
    ]
    formation_view = await formation.formation_participation(session, event, actor, now)
    return schemas.EventDetail(
        **summary.model_dump(),
        description_md=event.description_md,
        website_url=event.website_url,
        registration_url=event.registration_url,
        language=list(event.language),
        eligibility=event.eligibility,
        themes=list(event.themes),
        team_size_min=event.team_size_min,
        team_size_max=event.team_size_max,
        prize=Prize.model_validate(event.prize) if event.prize else None,
        difficulty_rationale=event.difficulty_rationale,
        review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
        created_by=event.created_by,
        confidence=float(event.confidence) if event.confidence is not None else None,
        unmapped_areas=list(event.unmapped_areas),
        sources=sources,
        members_going=members_going,
        formation=formation_view,
        my_lead_fit=formation.lead_eligibility(state.lead_fit, settings)
        if event.formation_enabled
        else None,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


async def create_manual_event(
    session: AsyncSession,
    jobs: procrastinate.App,
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
    await enqueue_lead_fits(session, jobs, event)
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


async def visible_event(session: AsyncSession, event_id: UUID, actor: models.User) -> models.Event:
    event = await load_event(session, event_id)
    if not actor.is_admin:
        if event.status in (EventStatus.DRAFT, EventStatus.ARCHIVED):
            raise ProblemError(ErrorCode.NOT_FOUND)
        if event.status == EventStatus.CANCELLED:
            involved = await session.scalar(
                select(
                    exists().where(
                        models.EventAttendance.event_id == event_id,
                        models.EventAttendance.user_id == actor.id,
                    )
                    | exists().where(
                        models.Enrollment.event_id == event_id,
                        models.Enrollment.user_id == actor.id,
                    )
                    | exists().where(
                        models.ProjectMember.event_id == event_id,
                        models.ProjectMember.user_id == actor.id,
                        models.ProjectMember.left_at.is_(None),
                    )
                )
            )
            if not involved:
                raise ProblemError(ErrorCode.NOT_FOUND)
    return event


async def list_events(
    session: AsyncSession,
    pagination: CursorParams,
    actor: models.User,
    now: datetime,
    *,
    kind: EventKind | None = None,
    phase: EventPhase | None = None,
    formation: bool | None = None,
    participation_phase: FormationPhase | None = None,
    area: str | None = None,
    format: EventFormat | None = None,
    q: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    mine: bool | None = None,
    origin: EventOrigin | None = None,
) -> schemas.EventSummaryPage:
    involved = (
        select(models.EventAttendance.id)
        .where(
            models.EventAttendance.event_id == models.Event.id,
            models.EventAttendance.user_id == actor.id,
        )
        .exists()
        | select(models.Enrollment.id)
        .where(
            models.Enrollment.event_id == models.Event.id,
            models.Enrollment.user_id == actor.id,
        )
        .exists()
        | select(models.ProjectMember.id)
        .where(
            models.ProjectMember.event_id == models.Event.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
        .exists()
    )
    statement = select(models.Event).where(
        or_(
            models.Event.status == EventStatus.PUBLISHED,
            and_(models.Event.status == EventStatus.CANCELLED, involved),
        )
    )
    if kind is not None:
        statement = statement.where(models.Event.kind == kind.value)
    if formation is not None:
        statement = statement.where(models.Event.formation_enabled.is_(formation))
    if origin is not None:
        statement = statement.where(models.Event.origin == origin.value)
    if format is not None:
        statement = statement.where(models.Event.format == format.value)
    if phase is not None:
        reg_deadline = func.coalesce(models.Event.registration_deadline, models.Event.starts_at)
        opened = or_(
            models.Event.registration_opens_at.is_(None),
            models.Event.registration_opens_at <= now,
        )
        phase_filters = {
            EventPhase.UPCOMING: [models.Event.registration_opens_at > now],
            EventPhase.REGISTRATION_OPEN: [opened, reg_deadline > now],
            EventPhase.REGISTRATION_CLOSED: [
                opened,
                reg_deadline <= now,
                models.Event.starts_at > now,
            ],
            EventPhase.ONGOING: [
                opened,
                reg_deadline <= now,
                models.Event.starts_at <= now,
                models.Event.ends_at > now,
            ],
            EventPhase.ENDED: [
                opened,
                reg_deadline <= now,
                models.Event.starts_at <= now,
                models.Event.ends_at <= now,
            ],
        }
        statement = statement.where(*phase_filters[phase])
    if participation_phase is not None:
        statement = statement.where(
            select(models.EventFormation.event_id)
            .where(
                models.EventFormation.event_id == models.Event.id,
                models.EventFormation.phase == participation_phase.value,
            )
            .exists()
        )
    if area is not None:
        statement = statement.where(
            select(models.EventArea.event_id)
            .join(models.Area, models.Area.id == models.EventArea.area_id)
            .where(models.EventArea.event_id == models.Event.id, models.Area.key == area)
            .exists()
        )
    if q is not None:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = statement.where(
            or_(
                models.Event.title.ilike(pattern, escape="\\"),
                models.Event.summary.ilike(pattern, escape="\\"),
            )
        )
    if from_ is not None:
        statement = statement.where(models.Event.ends_at >= from_)
    if to is not None:
        statement = statement.where(models.Event.starts_at <= to)
    if mine is True:
        statement = statement.where(involved)
    statement = apply_keyset(
        statement,
        pagination,
        sort=[models.Event.starts_at],
        id_column=models.Event.id,
        descending=True,
    )
    rows = list(await session.scalars(statement))
    items = rows[: pagination.limit]
    if not items:
        return schemas.EventSummaryPage(items=[], next_cursor=None)
    next_cursor = (
        encode_cursor([items[-1].starts_at, items[-1].id]) if len(rows) > pagination.limit else None
    )
    settings = await load_settings(session)
    event_ids = [item.id for item in items]
    areas = await area_details(session, event_ids)
    counts = await attendance_counts(session, event_ids)
    phases = await formation_phases(session, event_ids)
    states = await participation_states(session, items, actor, now)
    return schemas.EventSummaryPage(
        items=[
            event_summary(
                item,
                actor=actor,
                now=now,
                settings=settings,
                areas=areas.get(item.id, []),
                counts=counts[item.id],
                attendance=states[item.id].attendance,
                participation_phase=phases.get(item.id),
                state=states[item.id],
            )
            for item in items
        ],
        next_cursor=next_cursor,
    )


async def event_recipients(session: AsyncSession, event_id: UUID) -> list[UUID]:
    recipients = await session.scalars(
        select(models.EventAttendance.user_id)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.status == AttendanceStatus.GOING,
        )
        .union(
            select(models.Enrollment.user_id).where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status.in_(LIVE_ENROLLMENTS),
            ),
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.event_id == event_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            ),
        )
    )
    return sorted(recipients, key=str)


async def notify_event_changed(
    session: AsyncSession,
    event: models.Event,
    changes: Sequence[NotificationChange],
    now: datetime,
) -> None:
    if not changes:
        return
    recipients = await event_recipients(session, event.id)
    if not recipients:
        return
    fields_hash = sha1("|".join(sorted(change.field for change in changes)).encode()).hexdigest()[
        :12
    ]
    base = f"event_changed:{event.id}:{int(now.timestamp())}:{fields_hash}"
    payload = EventChangedPayload(
        link=f"/events/{event.slug}",
        event_id=event.id,
        event_title=event.title,
        changes=tuple(changes),
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        timezone=event.timezone,
    )
    for user_id in recipients:
        await notify(session, user_id, payload, now, dedupe_key=f"{base}:{user_id}")


async def notify_event_cancelled(
    session: AsyncSession,
    event: models.Event,
    recipients: Sequence[UUID],
    reason: str,
    now: datetime,
) -> None:
    if not recipients:
        return
    payload = EventCancelledPayload(
        link=f"/events/{event.slug}",
        event_id=event.id,
        event_title=event.title,
        reason=reason,
    )
    for user_id in recipients:
        await notify(
            session,
            user_id,
            payload,
            now,
            dedupe_key=f"event_cancelled:{event.id}:{user_id}",
        )


async def cancel_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    *,
    reason: str,
    actor: UUID | None,
    actor_kind: ActorKind,
    now: datetime,
) -> models.Event:
    event = await load_event(session, event_id, for_update=True)
    if event.status == EventStatus.ARCHIVED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Archived events cannot be cancelled")
    if event.status == EventStatus.CANCELLED:
        return event
    previous_status = event.status
    recipients = await event_recipients(session, event_id)
    cancellable_teams = [
        TeamStatus.FORMING,
        TeamStatus.REVIEW,
        TeamStatus.LOCKED,
        TeamStatus.ACTIVE,
    ]
    team_ids = list(
        await session.scalars(
            select(models.Project.id).where(
                models.Project.event_id == event_id,
                models.Project.team_status.in_(cancellable_teams),
            )
        )
    )
    event.status = EventStatus.CANCELLED.value
    await session.execute(
        update(models.EventFormation)
        .where(models.EventFormation.event_id == event_id)
        .values(phase=FormationPhase.CLOSED.value)
    )
    await session.execute(
        update(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.status == AttendanceStatus.GOING,
        )
        .values(status=AttendanceStatus.CANCELLED.value, status_at=now)
    )
    await session.execute(
        update(models.Project)
        .where(
            models.Project.event_id == event_id,
            models.Project.team_status.in_(cancellable_teams),
        )
        .values(team_status=TeamStatus.CANCELLED.value, status=ProjectStatus.CANCELLED.value)
    )
    await session.execute(
        update(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.status.in_(LIVE_ENROLLMENTS),
        )
        .values(status=EnrollmentStatus.EXPIRED.value, status_at=now)
    )
    await session.execute(
        update(models.Invite)
        .where(models.Invite.event_id == event_id, models.Invite.status == InviteStatus.PENDING)
        .values(status=InviteStatus.CLOSED.value, closed_reason="event_cancelled")
    )
    for model, pending, closed in (
        (models.JoinRequest, JoinRequestStatus.PENDING.value, JoinRequestStatus.CLOSED.value),
        (models.SwapRequest, SwapRequestStatus.PENDING.value, SwapRequestStatus.CLOSED.value),
        (models.MergeProposal, MergeProposalStatus.PENDING.value, MergeProposalStatus.CLOSED.value),
        (models.LeadOffer, LeadOfferStatus.PENDING.value, LeadOfferStatus.CLOSED.value),
    ):
        await session.execute(
            update(model)
            .where(model.event_id == event_id, model.status == pending)
            .values(status=closed)
        )
    await notify_event_cancelled(session, event, recipients, reason, now)
    if team_ids:
        await raise_alert(
            session,
            AlertKind.EVENT_CANCELLED_WITH_TEAMS,
            now,
            title=f"Event cancelled with teams: {event.title}",
            message=f"{len(team_ids)} teams were cancelled with the event",
            link=f"/admin/events/{event.id}",
            subject_type="event",
            subject_id=event.id,
            subject_title=event.title,
            dedupe_key=f"event_cancelled_with_teams:{event.id}",
        )
    await record_audit(
        session,
        actor=actor,
        actor_kind=actor_kind,
        action=AuditAction.EVENT_CANCEL,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={"status": previous_status},
        after={"status": EventStatus.CANCELLED.value},
        reason=reason,
    )
    await session.refresh(event)
    return event


def _json(value: Any) -> JsonValue:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal | UUID | Enum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return value


async def update_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.UpdateEventBody,
    actor: models.User,
    now: datetime,
) -> models.Event:
    event = await load_event(session, event_id, for_update=True)
    if body.status is not None and body.status != event.status:
        if body.status == EventStatus.CANCELLED:
            return await cancel_event(
                session,
                jobs,
                event_id,
                reason=body.reason or "Cancelled by admin",
                actor=actor.id,
                actor_kind=ActorKind.ADMIN,
                now=now,
            )
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Publishing and archiving use their own endpoints"
        )
    raw = body.model_dump(exclude_unset=True)
    raw.pop("reason", None)
    raw.pop("status", None)
    area_input = raw.pop("areas", None)
    for field in ("location", "prize"):
        if field in raw and (value := getattr(body, field)) is not None:
            raw[field] = value.model_dump(mode="json", exclude_none=True) or None
    if raw.get("difficulty") is not None:
        raw["difficulty"] = Decimal(str(raw["difficulty"])).quantize(Decimal("0.1"), ROUND_HALF_UP)
    if "slug" in raw and await session.scalar(
        select(exists().where(models.Event.slug == raw["slug"], models.Event.id != event_id))
    ):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Event slug already exists")
    for field in ("website_url", "registration_url"):
        if field in raw and raw[field] is not None:
            raw[field] = str(raw[field])
    for field in (
        "slug",
        "kind",
        "formation_enabled",
        "title",
        "starts_at",
        "ends_at",
        "timezone",
        "language",
        "themes",
    ):
        if field in raw and raw[field] is None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"{field} cannot be null")
    merged = {
        field: raw.get(field, getattr(event, field))
        for field in ("starts_at", "ends_at", "team_size_min", "team_size_max")
    }
    if merged["ends_at"] < merged["starts_at"]:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "ends_at must not precede starts_at")
    min_size, max_size = merged["team_size_min"], merged["team_size_max"]
    if min_size is not None and max_size is not None and min_size > max_size:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR, "team_size_min must not exceed team_size_max"
        )
    if any(size is not None and not 1 <= size <= 20 for size in (min_size, max_size)):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "team sizes must be between 1 and 20")
    if body.formation_enabled is True and not event.formation_enabled:
        if max_size is not None and max_size < 2:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Formation needs team_size_max of at least 2"
            )
        if await session.scalar(
            select(
                exists().where(
                    models.Enrollment.event_id == event_id,
                    models.Enrollment.status.in_(LIVE_ENROLLMENTS),
                )
                | exists().where(
                    models.Project.event_id == event_id,
                    models.Project.team_status.is_not(None),
                    models.Project.team_status.not_in(TEAM_TERMINAL),
                )
            )
        ):
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Formation can only be enabled before enrollments"
            )
    changes = {
        field: value for field, value in raw.items() if _json(value) != _json(getattr(event, field))
    }
    before_values = {field: getattr(event, field) for field in changes}
    resolved_areas: list[tuple[models.Area, EventAreaInput]] | None = None
    if area_input is not None:
        resolved_areas = await resolve_areas(
            session,
            [
                EventAreaInput(key=area.key, importance=area.importance, headcount=area.headcount)
                for area in body.areas or ()
            ],
        )
        old_areas = [
            {"key": key, "importance": importance, "headcount": headcount}
            for key, importance, headcount in await session.execute(
                select(models.Area.key, models.EventArea.importance, models.EventArea.headcount)
                .join(models.EventArea, models.EventArea.area_id == models.Area.id)
                .where(models.EventArea.event_id == event_id)
                .order_by(models.Area.key)
            )
        ]
        new_areas = [
            {
                "key": area.key,
                "importance": area_input.importance.value,
                "headcount": area_input.headcount,
            }
            for area, area_input in sorted(resolved_areas, key=lambda item: item[0].key)
        ]
        if old_areas != new_areas:
            changes["areas"] = new_areas
            before_values["areas"] = old_areas
    if not changes:
        return event
    previous_dates = formation.event_dates(event)
    for field, value in changes.items():
        if field != "areas":
            setattr(event, field, value.value if isinstance(value, Enum) else value)
    if "areas" in changes and resolved_areas is not None:
        await session.execute(delete(models.EventArea).where(models.EventArea.event_id == event_id))
        session.add_all(
            models.EventArea(
                event_id=event_id,
                area_id=area.id,
                importance=area_input.importance.value,
                headcount=area_input.headcount,
            )
            for area, area_input in resolved_areas
        )
    for field in sorted(changes.keys() - LOCKABLE_SKIP):
        await session.execute(
            insert(models.EventFieldLock)
            .values(event_id=event_id, field=field, locked_by=actor.id, locked_at=now)
            .on_conflict_do_nothing(index_elements=["event_id", "field"])
        )
    before = {field: _json(value) for field, value in before_values.items()}
    after = {field: _json(value) for field, value in changes.items()}
    diff: dict[str, JsonValue] = {
        field: {"before": before[field], "after": after[field]} for field in changes
    }
    session.add(
        models.EventRevision(
            event_id=event_id,
            origin=EventRevisionOrigin.ADMIN.value,
            actor_id=actor.id,
            diff=diff,
            ignored_fields=[],
        )
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_EDIT,
        entity_type=AuditEntityType.EVENT,
        entity_id=event_id,
        before=before,
        after=after,
        reason=body.reason,
    )
    if "formation_enabled" in changes and event.formation_enabled:
        await formation.ensure_formation(session, event, now)
    if changes.keys() & {"formation_enabled", "areas"}:
        await enqueue_lead_fits(session, jobs, event)
    notification_fields = changes.keys() & {
        "starts_at",
        "ends_at",
        "team_size_min",
        "team_size_max",
        "format",
        "registration_deadline",
    }
    if notification_fields:
        await notify_event_changed(
            session,
            event,
            [
                NotificationChange(
                    field=field,
                    previous=str(_json(before_values[field]))
                    if before_values[field] is not None
                    else None,
                    current=str(_json(getattr(event, field)))
                    if getattr(event, field) is not None
                    else None,
                )
                for field in sorted(notification_fields)
            ],
            now,
        )
    if changes.keys() & {"starts_at", "ends_at", "registration_deadline"}:
        await formation.dates_changed(session, jobs, event, previous_dates, now)
    from hmm_core.recommendations import service as recommendations

    await recommendations.enqueue_event_refresh(session, jobs, event_id)
    await session.flush()
    await session.refresh(event)
    return event


async def list_locks(session: AsyncSession, event_id: UUID) -> list[schemas.EventFieldLock]:
    await load_event(session, event_id)
    return [
        schemas.EventFieldLock(
            event_id=lock.event_id,
            field=lock.field,
            locked_by=lock.locked_by,
            locked_at=lock.locked_at,
            allowed_actions=[schemas.EventFieldLockAction.UNLOCK],
        )
        for lock in await session.scalars(
            select(models.EventFieldLock)
            .where(models.EventFieldLock.event_id == event_id)
            .order_by(
                models.EventFieldLock.field,
                models.EventFieldLock.locked_by,
                models.EventFieldLock.locked_at,
            )
        )
    ]


async def unlock_field(
    session: AsyncSession,
    event_id: UUID,
    field: str,
    actor: models.User,
    reason: str | None,
    now: datetime,
) -> None:
    event = await load_event(session, event_id)
    lock = await session.scalar(
        select(models.EventFieldLock)
        .where(models.EventFieldLock.event_id == event_id, models.EventFieldLock.field == field)
        .with_for_update()
    )
    if lock is None:
        raise ProblemError(ErrorCode.NOT_FOUND, "Field is not locked")
    await session.delete(lock)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_UNLOCK_FIELD,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={
            "field": field,
            "locked_by": str(lock.locked_by),
            "locked_at": lock.locked_at.isoformat(),
        },
        after=None,
        reason=reason,
    )


async def list_revisions(session: AsyncSession, event_id: UUID) -> list[schemas.EventRevision]:
    await load_event(session, event_id)
    return [
        schemas.EventRevision(
            id=revision.id,
            event_id=revision.event_id,
            origin=EventRevisionOrigin(revision.origin),
            actor_id=revision.actor_id,
            diff=TypeAdapter(dict[str, JsonValue]).validate_python(revision.diff),
            ignored_fields=list(revision.ignored_fields),
            created_at=revision.created_at,
        )
        for revision in await session.scalars(
            select(models.EventRevision)
            .where(models.EventRevision.event_id == event_id)
            .order_by(models.EventRevision.created_at.desc(), models.EventRevision.id.desc())
        )
    ]


async def list_event_projects(
    session: AsyncSession, event_id: UUID, actor: models.User, now: datetime
) -> list[ProjectView]:
    event = await visible_event(session, event_id, actor)
    rows = await session.scalars(
        select(models.Project)
        .where(models.Project.event_id == event.id)
        .order_by(models.Project.team_status.is_(None), models.Project.name, models.Project.id)
    )
    return await projects.project_views(session, list(rows), actor, now)


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


async def match_person_user(session: AsyncSession, person: SourcePerson) -> models.User | None:
    if person.github_login:
        user = await session.scalar(
            select(models.User)
            .where(
                func.lower(models.User.github_login) == person.github_login.lower(),
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.id)
            .limit(1)
        )
        if user is not None:
            return user
    if person.telegram_username:
        return await session.scalar(
            select(models.User)
            .join(models.TelegramLink, models.TelegramLink.user_id == models.User.id)
            .where(
                func.lower(models.TelegramLink.username) == person.telegram_username.lower(),
                models.TelegramLink.broken_at.is_(None),
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.id)
            .limit(1)
        )
    return None


async def upsert_source_person(
    session: AsyncSession,
    event: models.Event,
    source_id: str,
    person: SourcePerson,
    matched_user_id: UUID | None,
) -> models.EventSourcePerson:
    identity = person.github_login or person.telegram_username or person.display_name
    row = await session.scalar(
        select(models.EventSourcePerson)
        .where(
            models.EventSourcePerson.source_id == source_id,
            func.coalesce(
                models.EventSourcePerson.github_login,
                models.EventSourcePerson.telegram_username,
                models.EventSourcePerson.display_name,
            )
            == identity,
        )
        .with_for_update()
    )
    if row is None:
        row = models.EventSourcePerson(
            event_id=event.id,
            source_id=source_id,
            github_login=person.github_login,
            telegram_username=person.telegram_username,
            display_name=person.display_name[:100],
            role=person.role.value,
            place=person.place,
            matched_user_id=matched_user_id,
        )
        session.add(row)
    else:
        row.display_name = person.display_name[:100]
        row.role = person.role.value
        row.place = person.place
        row.matched_user_id = matched_user_id or row.matched_user_id
    await session.flush()
    return row


async def apply_attendance_fact(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    person: SourcePerson,
    user: models.User,
    now: datetime,
) -> models.EventAttendance | None:
    role = (
        AttendanceRole.ATTENDEE
        if person.role == SourcePersonRole.WINNER
        else AttendanceRole(person.role.value)
    )
    attendance = await session.scalar(
        select(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event.id,
            models.EventAttendance.user_id == user.id,
        )
        .with_for_update()
    )
    if attendance is None:
        attendance = models.EventAttendance(
            event_id=event.id,
            user_id=user.id,
            status=AttendanceStatus.ATTENDED.value,
            role=role.value,
            proof_links=[],
            note=None,
            trust=Trust.SELF.value,
            status_at=now,
        )
        session.add(attendance)
    elif attendance.status == AttendanceStatus.CANCELLED:
        return None
    else:
        if attendance.status == AttendanceStatus.GOING:
            attendance.status = AttendanceStatus.ATTENDED.value
            attendance.status_at = now
        if attendance.role == AttendanceRole.ATTENDEE and role != AttendanceRole.ATTENDEE:
            attendance.role = role.value
    await session.flush()
    await feed.add_confirmation(
        session,
        jobs,
        fact_type=ConfirmationFactType.ATTENDANCE,
        fact_id=attendance.id,
        subject_user_id=user.id,
        confirmer_id=None,
        source=ConfirmationSource.SYSTEM,
        now=now,
    )
    return attendance


async def team_project_id(session: AsyncSession, event_id: UUID, user_id: UUID) -> UUID | None:
    return await session.scalar(
        select(models.ProjectMember.project_id)
        .where(
            models.ProjectMember.event_id == event_id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.is_team_row,
        )
        .order_by(models.ProjectMember.joined_at.desc(), models.ProjectMember.id.desc())
        .limit(1)
    )


async def apply_winner_places(
    session: AsyncSession, places: dict[UUID, int], now: datetime
) -> None:
    for project_id, place in places.items():
        result = await session.scalar(
            select(models.ProjectResult)
            .where(models.ProjectResult.project_id == project_id)
            .with_for_update()
        )
        if (
            result is None
            or result.outcome != ResultOutcome.FINISHED.value
            or result.place_status == PlaceStatus.REJECTED.value
        ):
            continue
        result.place = place
        result.place_status = PlaceStatus.VERIFIED.value
        result.place_verified_by = None
        result.place_verified_at = now


async def sync_people(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    item: EventImport,
    now: datetime,
) -> None:
    if event.status in (EventStatus.CANCELLED.value, EventStatus.ARCHIVED.value):
        return
    places: dict[UUID, int] = {}
    for person in item.people:
        user = await match_person_user(session, person)
        await upsert_source_person(
            session, event, item.source.source_id, person, user.id if user else None
        )
        if user is None:
            continue
        await apply_attendance_fact(session, jobs, event, person, user, now)
        if person.role == SourcePersonRole.WINNER and person.place is not None:
            project_id = await team_project_id(session, event.id, user.id)
            if project_id is not None:
                places[project_id] = min(places.get(project_id, person.place), person.place)
    await apply_winner_places(session, places, now)


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
