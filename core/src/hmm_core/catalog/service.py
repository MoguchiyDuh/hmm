import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha1
from typing import Self
from uuid import UUID

import procrastinate
from hmm_contracts.algo import EventArea, EventSnapshot
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventPhase,
    EventReviewReason,
    EventStatus,
    FormationPhase,
    InviteStatus,
    JoinRequestStatus,
    LeadOfferStatus,
    MergeProposalStatus,
    ProjectStatus,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_contracts.imports import Location, Prize
from hmm_contracts.notifications import (
    AlertKind,
    EventCancelledPayload,
    EventChangedPayload,
    NotificationChange,
)
from pydantic import JsonValue
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance.service import attendance_view
from hmm_core.catalog import schemas
from hmm_core.formation import service as formation
from hmm_core.notifications.service import notify, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import EventAction, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings

SLUG_MAX = 100
LIVE_ENROLLMENTS = frozenset(
    {EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE}
)
TEAM_TERMINAL = frozenset({TeamStatus.DISBANDED, TeamStatus.CANCELLED})


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
