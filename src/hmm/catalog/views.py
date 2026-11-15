from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.attendance.service import attendance_view
from hmm.catalog import schemas
from hmm.catalog.changes import LIVE_ENROLLMENTS
from hmm.catalog.import_schema import Location, Prize
from hmm.common import (
    AreaImportance,
    AttendanceStatus,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventPhase,
    EventReviewReason,
    EventStatus,
    FormationPhase,
)
from hmm.formation import queue as formation_queue
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.pagination import CursorParams, keyset_rows
from hmm.platform.schemas import EventAction, user_ref
from hmm.platform.settings import RuntimeSettings, load_settings
from hmm.projects import views as projects_views
from hmm.projects.schemas import ProjectView

SLUG_MAX = 100


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


@dataclass(frozen=True, slots=True)
class EventParticipation:
    attendance: models.EventAttendance | None
    enrollment: models.Enrollment | None
    on_team: bool
    phase: FormationPhase | None
    formation_at: datetime | None


async def participation_states(
    session: AsyncSession, events: Sequence[models.Event], actor: models.User
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
    formation_open = state.phase in (FormationPhase.OPEN, FormationPhase.FORMED)
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
        if now < reg_deadline:
            allowed.add(EventAction.CREATE_PARTY)
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
    state = (await participation_states(session, [event], actor))[event.id]
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
        user_ref(user)
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
    formation_view = await formation_queue.formation_participation(session, event, actor, now)
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
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


async def visible_event(session: AsyncSession, event_id: UUID, actor: models.User) -> models.Event:
    event = await get_or_404(session, models.Event, event_id)
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
    items, next_cursor = await keyset_rows(
        session, statement, pagination, sort=[models.Event.starts_at], id_column=models.Event.id
    )
    if not items:
        return schemas.EventSummaryPage(items=[], next_cursor=None)
    settings = await load_settings(session)
    event_ids = [item.id for item in items]
    areas = await area_details(session, event_ids)
    counts = await attendance_counts(session, event_ids)
    phases = await formation_phases(session, event_ids)
    states = await participation_states(session, items, actor)
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


async def list_event_projects(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> list[ProjectView]:
    event = await visible_event(session, event_id, actor)
    rows = await session.scalars(
        select(models.Project)
        .where(models.Project.event_id == event.id)
        .order_by(models.Project.team_status.is_(None), models.Project.name, models.Project.id)
    )
    return await projects_views.project_views(session, list(rows), actor)
