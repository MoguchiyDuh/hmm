from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    EnrollmentStatus,
    ErrorCode,
    EventStatus,
    FormationPhase,
    MemberState,
    OnboardingStatus,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
)
from hmm_contracts.notifications import NotificationPerson
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import service as projects

LIVE_ENROLLMENT_STATES: frozenset[EnrollmentStatus] = frozenset(
    {EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE}
)


@dataclass(frozen=True, slots=True)
class FormationSizes:
    min_size: int
    max_size: int
    target: int
    viable: int
    core_max: int


def formation_sizes(
    event: models.Event, formation: models.EventFormation, settings: RuntimeSettings
) -> FormationSizes:
    min_size = event.team_size_min or settings.team_size_default_min
    max_size = event.team_size_max or settings.team_size_default_max
    if min_size > max_size:
        max_size = min_size
    if max_size < 2:
        max_size = 2
    target = formation.target_override or max_size
    target = min(max(target, min_size), max_size)
    return FormationSizes(
        min_size=min_size,
        max_size=max_size,
        target=target,
        viable=min(max_size, max(min_size, ceil(target * settings.viable_ratio))),
        core_max=min(max_size, max(2, ceil(target * settings.core_ratio))),
    )


CAPACITY_TEAM_STATES = frozenset({TeamStatus.FORMING, TeamStatus.REVIEW, TeamStatus.LOCKED})


ONBOARDED_STATUSES: frozenset[str] = frozenset(
    {OnboardingStatus.COMPLETED.value, OnboardingStatus.REOPENED.value}
)


LEAD_FIT_PHASES: frozenset[FormationPhase] = frozenset({FormationPhase.OPEN, FormationPhase.LOCKED})


TERMINAL_PROJECT_STATUSES = frozenset(
    {ProjectStatus.ABANDONED, ProjectStatus.DISBANDED, ProjectStatus.CANCELLED}
)


ACCEPT_LABEL = "notifications.actions.accept"


DECLINE_LABEL = "notifications.actions.decline"


REJECT_LABEL = "notifications.actions.reject"


CLOSED_TEAM_STATUSES: frozenset[str] = projects.TERMINAL_TEAM_STATUSES


REMOVABLE_TEAM_STATUSES: frozenset[str] = frozenset(
    {TeamStatus.FORMING.value, TeamStatus.REVIEW.value, TeamStatus.LOCKED.value}
)


RESULT_TEAM_STATUSES: frozenset[str] = frozenset(
    {TeamStatus.FINISHED.value, TeamStatus.WITHDRAWN.value}
)


FORMATION_ALGO_LIMIT = 100


FORCEABLE_TEAM_STATUSES: frozenset[str] = frozenset(
    {TeamStatus.LOCKED.value, TeamStatus.ACTIVE.value}
)


async def _own_team_row(
    session: AsyncSession, project_id: UUID, user_id: UUID
) -> models.ProjectMember | None:
    return await session.scalar(
        select(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project_id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.is_team_row.is_(True),
        )
        .execution_options(populate_existing=True)
    )


async def note_lead_action(
    session: AsyncSession, project: models.Project, actor: models.User, now: datetime
) -> models.ProjectMember | None:
    if project.lead_user_id != actor.id:
        return None
    project.last_lead_action_at = now
    member = await _own_team_row(session, project.id, actor.id)
    if member is None or member.state != MemberState.PROPOSED.value:
        await session.flush()
        return None
    member.state = MemberState.CONFIRMED.value
    member.confirmed_at = now
    member.confirm_by = None
    member.confirm_kind = None
    await session.flush()
    return member


async def departed_team_user_ids(
    session: AsyncSession, project_id: UUID, user_ids: Sequence[UUID]
) -> set[UUID]:
    if not user_ids:
        return set()
    return set(
        await session.scalars(
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id.in_(list(user_ids)),
                models.ProjectMember.left_at.is_not(None),
            )
        )
    )


@dataclass(frozen=True, slots=True)
class FormationTiming:
    reg_deadline: datetime
    team_deadline: datetime
    formation_at: datetime
    review_ends_at: datetime


def formation_timing(
    event: models.Event, settings: RuntimeSettings, now: datetime
) -> FormationTiming:
    reg_deadline = event.registration_deadline or event.starts_at
    team_deadline = reg_deadline - timedelta(hours=settings.registration_buffer_hours)
    formation_at = team_deadline - timedelta(hours=settings.formation_lead_hours)
    min_notice = timedelta(hours=settings.formation_min_notice_hours)
    review_min = timedelta(hours=settings.review_min_hours)
    if formation_at < now + min_notice:
        formation_at = min(now + min_notice, team_deadline - review_min)
        if formation_at < now:
            team_deadline = reg_deadline - timedelta(hours=1)
            formation_at = min(now + min_notice, team_deadline - review_min)
        if formation_at < now:
            formation_at = now + timedelta(hours=1)
    return FormationTiming(
        reg_deadline=reg_deadline,
        team_deadline=team_deadline,
        formation_at=formation_at,
        review_ends_at=min(formation_at + timedelta(hours=settings.review_hours), team_deadline),
    )


async def ensure_formation(
    session: AsyncSession, event: models.Event, now: datetime
) -> models.EventFormation | None:
    if not event.formation_enabled:
        return None
    existing = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == event.id)
        .with_for_update()
    )
    if existing is not None:
        return existing
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    formation = models.EventFormation(
        event_id=event.id,
        phase=FormationPhase.OPEN.value,
        formation_at=timing.formation_at,
        review_ends_at=timing.review_ends_at,
    )
    session.add(formation)
    await session.flush()
    return formation


@dataclass(frozen=True, slots=True)
class EventDates:
    starts_at: datetime
    ends_at: datetime
    registration_deadline: datetime | None


def event_dates(event: models.Event) -> EventDates:
    return EventDates(
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        registration_deadline=event.registration_deadline,
    )


async def dates_changed(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    previous: EventDates,
    now: datetime,
) -> None:
    return None


def lead_eligibility(
    fit: models.LeadFit | None, settings: RuntimeSettings
) -> schemas.LeadEligibility | None:
    if fit is None:
        return None
    return schemas.LeadEligibility(
        fit=float(fit.fit),
        eligible=float(fit.fit) >= settings.lead_fit_threshold,
        reason=fit.reason,
        computed_at=fit.computed_at,
    )


def dnd_active(user: models.User, now: datetime) -> bool:
    return user.dnd_indefinite or (user.dnd_until is not None and user.dnd_until > now)


def effective_score(score: models.AreaScore) -> Decimal:
    return score.override if score.override is not None else score.calculated


def score_reason(score: models.AreaScore) -> str:
    if score.override is not None and score.override_reason is not None:
        return score.override_reason
    return score.calc_reason


def projected_team_sizes(
    placed: int, cores: list[int], new_teams: int, v: int, t: int, max_size: int
) -> list[int]:
    sizes = [*cores, *([0] * new_teams)]
    left = placed
    for cap in (v, t, max_size):
        while left:
            below = [index for index, size in enumerate(sizes) if size < cap]
            if not below:
                break
            for index in below:
                if not left:
                    break
                sizes[index] += 1
                left -= 1
    return sizes


async def check_event_visible(
    session: AsyncSession, event: models.Event, actor: models.User
) -> None:
    if actor.is_admin:
        return None
    if event.status in (EventStatus.DRAFT, EventStatus.ARCHIVED):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if event.status == EventStatus.CANCELLED:
        involved = await session.scalar(
            select(
                exists().where(
                    models.EventAttendance.event_id == event.id,
                    models.EventAttendance.user_id == actor.id,
                )
                | exists().where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.user_id == actor.id,
                )
                | exists().where(
                    models.ProjectMember.event_id == event.id,
                    models.ProjectMember.user_id == actor.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row,
                )
            )
        )
        if not involved:
            raise ProblemError(ErrorCode.NOT_FOUND)
    return None


async def formation_event(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> models.Event:
    event = await session.scalar(select(models.Event).where(models.Event.id == event_id))
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await check_event_visible(session, event, actor)
    return event


async def locked_formation_event(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> models.Event:
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await check_event_visible(session, event, actor)
    return event


async def formation_row(session: AsyncSession, event_id: UUID) -> models.EventFormation:
    formation = await session.get(models.EventFormation, event_id)
    if formation is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation is not initialised")
    return formation


async def invite_project(
    session: AsyncSession, project_id: UUID, actor: models.User, *, lock: bool
) -> tuple[models.Project, models.Event | None]:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    if project.team_status is not None and project.event_id is not None:
        if lock:
            event = await locked_formation_event(session, project.event_id, actor)
        else:
            event = await formation_event(session, project.event_id, actor)
    if lock:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is None:
        live = await session.scalar(
            select(models.ProjectMember.id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
            .limit(1)
        )
        if (
            project.visibility == ProjectVisibility.MEMBERS.value
            and not actor.is_admin
            and actor.id != project.lead_user_id
            and live is None
        ):
            raise ProblemError(ErrorCode.NOT_FOUND)
    return project, event


async def _enrollment_row(
    session: AsyncSession, event_id: UUID, user_id: UUID, *, live: bool, for_update: bool
) -> models.Enrollment | None:
    states = sorted(member.value for member in LIVE_ENROLLMENT_STATES)
    query = (
        select(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.user_id == user_id,
            models.Enrollment.status.in_(states)
            if live
            else models.Enrollment.status.notin_(states),
        )
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        .limit(1)
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def live_enrollment_row(
    session: AsyncSession, event_id: UUID, user_id: UUID, *, for_update: bool = False
) -> models.Enrollment | None:
    return await _enrollment_row(session, event_id, user_id, live=True, for_update=for_update)


async def latest_terminal_enrollment(
    session: AsyncSession, event_id: UUID, user_id: UUID, *, for_update: bool = False
) -> models.Enrollment | None:
    return await _enrollment_row(session, event_id, user_id, live=False, for_update=for_update)


async def enqueue_participation_jobs(
    session: AsyncSession, jobs: procrastinate.App, event_id: UUID, *, reserve: bool
) -> None:
    if reserve:
        await defer_job(session, jobs, JobName.RANK_RESERVE, event_id=str(event_id))
    project_ids = list(
        await session.scalars(
            select(models.Project.id)
            .where(
                models.Project.event_id == event_id,
                models.Project.team_status.in_(
                    sorted(member.value for member in CAPACITY_TEAM_STATES)
                ),
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    )
    for project_id in project_ids:
        await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project_id))
    return None


async def participation_event(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> tuple[models.Event, FormationPhase]:
    event = await locked_formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    phase = FormationPhase((await formation_row(session, event_id)).phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    return event, phase


def notification_person(user: models.User) -> NotificationPerson:
    return NotificationPerson(user_id=user.id, display_name=user.display_name)
