from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import LeadFit as AlgoLeadFit
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ConfirmKind,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventKind,
    EventStatus,
    FormationPhase,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeftReason,
    MemberRole,
    MemberState,
    OnboardingStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.formation import placement_bounds
from hmm_contracts.notifications import FormationRescheduledPayload, TeamDisbandedPayload
from sqlalchemy import Select, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import service as attendance
from hmm_core.formation import schemas
from hmm_core.notifications.service import notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import AreaScoreSummary, EventRef, ProjectRef, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects

LIVE_ENROLLMENT_STATES: frozenset[EnrollmentStatus] = frozenset(
    {EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE}
)
CAPACITY_TEAM_STATES = frozenset({TeamStatus.FORMING, TeamStatus.REVIEW, TeamStatus.LOCKED})
ONBOARDED_STATUSES: frozenset[str] = frozenset(
    {OnboardingStatus.COMPLETED.value, OnboardingStatus.REOPENED.value}
)
LEAD_FIT_PHASES: frozenset[FormationPhase] = frozenset({FormationPhase.OPEN, FormationPhase.LOCKED})
TERMINAL_PROJECT_STATUSES = frozenset(
    {ProjectStatus.ABANDONED, ProjectStatus.DISBANDED, ProjectStatus.CANCELLED}
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


def enrollment_actions(
    enrollment: models.Enrollment,
    *,
    event: models.Event,
    actor: models.User,
    on_team: bool,
    now: datetime,
) -> list[schemas.EnrollmentAction]:
    own_live = enrollment.user_id == actor.id and enrollment.status in LIVE_ENROLLMENT_STATES
    allowed: set[schemas.EnrollmentAction] = set()
    if own_live and now < event.starts_at:
        allowed.add(schemas.EnrollmentAction.EDIT)
        if not on_team:
            allowed.add(schemas.EnrollmentAction.WITHDRAW)
    return [action for action in schemas.EnrollmentAction if action in allowed]


def formation_actions(
    event: models.Event,
    formation: models.EventFormation,
    *,
    actor: models.User,
    enrollment: models.Enrollment | None,
    on_team: bool,
    lead_fit: models.LeadFit | None,
    now: datetime,
    reg_deadline: datetime,
    settings: RuntimeSettings,
) -> list[schemas.FormationAction]:
    phase = FormationPhase(formation.phase)
    published = event.status == EventStatus.PUBLISHED
    live = enrollment is not None and enrollment.status in LIVE_ENROLLMENT_STATES
    before_start = now < event.starts_at
    allowed: set[schemas.FormationAction] = set()
    if (
        published
        and phase not in (FormationPhase.FORMING, FormationPhase.CLOSED)
        and now < reg_deadline
        and before_start
        and not on_team
        and not live
    ):
        allowed.add(schemas.FormationAction.ENROLL)
    if live and before_start:
        allowed.add(schemas.FormationAction.EDIT_ENROLLMENT)
        if not on_team:
            allowed.add(schemas.FormationAction.WITHDRAW)
    if (
        published
        and phase == FormationPhase.OPEN
        and not on_team
        and not dnd_active(actor, now)
        and lead_fit is not None
        and float(lead_fit.fit) >= settings.lead_fit_threshold
    ):
        allowed.add(schemas.FormationAction.CLAIM_LEAD)
    if actor.is_admin and phase == FormationPhase.OPEN:
        allowed.add(schemas.FormationAction.EDIT)
    if actor.is_admin and phase in (FormationPhase.OPEN, FormationPhase.REVIEW):
        allowed.add(schemas.FormationAction.RUN)
    return [action for action in schemas.FormationAction if action in allowed]


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


async def live_membership(
    session: AsyncSession, event_id: UUID, user_id: UUID
) -> tuple[models.ProjectMember, models.Project] | None:
    row = (
        await session.execute(
            select(models.ProjectMember, models.Project)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.ProjectMember.event_id == event_id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def membership_view(
    session: AsyncSession, member: models.ProjectMember, actor: models.User, now: datetime
) -> project_schemas.ProjectMember:
    area_key = (
        None
        if member.area_id is None
        else await session.scalar(select(models.Area.key).where(models.Area.id == member.area_id))
    )
    user = await session.get_one(models.User, member.user_id)
    actions = (
        [
            project_schemas.ProjectMemberAction.CONFIRM_PLACEMENT,
            project_schemas.ProjectMemberAction.DECLINE_PLACEMENT,
        ]
        if member.user_id == actor.id and member.state == MemberState.PROPOSED
        else []
    )
    return project_schemas.ProjectMember(
        id=member.id,
        project_id=member.project_id,
        event_id=member.event_id,
        user_id=member.user_id,
        user=UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url),
        role=MemberRole(member.role),
        area_key=area_key,
        state=MemberState(member.state),
        trust=Trust(member.trust),
        joined_via=JoinedVia(member.joined_via),
        proposal_reason=member.proposal_reason,
        confirm_by=member.confirm_by,
        confirm_kind=ConfirmKind(member.confirm_kind) if member.confirm_kind else None,
        is_team_row=member.is_team_row,
        joined_at=member.joined_at,
        confirmed_at=member.confirmed_at,
        left_at=member.left_at,
        left_reason=LeftReason(member.left_reason) if member.left_reason else None,
        allowed_actions=actions,
    )


async def enrollment_views(
    session: AsyncSession,
    enrollments: Sequence[models.Enrollment],
    actor: models.User,
    now: datetime,
) -> list[schemas.Enrollment]:
    if not enrollments:
        return []
    user_ids = {enrollment.user_id for enrollment in enrollments}
    event_ids = {enrollment.event_id for enrollment in enrollments}
    area_ids = {
        enrollment.preferred_area_id
        for enrollment in enrollments
        if enrollment.preferred_area_id is not None
    }
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    events = {
        event.id: event
        for event in await session.scalars(
            select(models.Event).where(models.Event.id.in_(event_ids))
        )
    }
    areas = (
        {
            area.id: area
            for area in await session.scalars(
                select(models.Area).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    teams = {
        (event_id, user_id): project
        for event_id, user_id, project in await session.execute(
            select(models.ProjectMember.event_id, models.ProjectMember.user_id, models.Project)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.ProjectMember.event_id.in_(event_ids),
                models.ProjectMember.user_id.in_(user_ids),
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            )
        )
    }
    scores: dict[UUID, list[AreaScoreSummary]] = {}
    for score, area in await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id.in_(user_ids), models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    ):
        scores.setdefault(score.user_id, []).append(
            AreaScoreSummary(
                area_key=area.key,
                effective=float(effective_score(score)),
                reason=score_reason(score),
            )
        )
    top_scores = {
        user_id: sorted(rows, key=lambda row: (-row.effective, row.area_key))[:3]
        for user_id, rows in scores.items()
    }
    views: list[schemas.Enrollment] = []
    for enrollment in enrollments:
        event = events[enrollment.event_id]
        user = users[enrollment.user_id]
        project = teams.get((enrollment.event_id, enrollment.user_id))
        views.append(
            schemas.Enrollment(
                id=enrollment.id,
                event_id=enrollment.event_id,
                event=EventRef(
                    id=event.id,
                    title=event.title,
                    kind=EventKind(event.kind),
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    timezone=event.timezone,
                ),
                user=UserRef(
                    id=user.id, display_name=user.display_name, avatar_url=user.avatar_url
                ),
                preferred_area_key=(
                    areas[enrollment.preferred_area_id].key
                    if enrollment.preferred_area_id is not None
                    and enrollment.preferred_area_id in areas
                    else None
                ),
                note=enrollment.note,
                willing_to_lead=enrollment.willing_to_lead,
                status=EnrollmentStatus(enrollment.status),
                source=EnrollmentSource(enrollment.source),
                reserve_rank=enrollment.reserve_rank,
                status_at=enrollment.status_at,
                created_at=enrollment.created_at,
                updated_at=enrollment.updated_at,
                top_scores=top_scores.get(enrollment.user_id, []),
                team=(
                    ProjectRef(
                        id=project.id,
                        name=project.name,
                        event_id=project.event_id,
                        team_status=TeamStatus(project.team_status)
                        if project.team_status
                        else None,
                    )
                    if project
                    else None
                ),
                allowed_actions=enrollment_actions(
                    enrollment, event=event, actor=actor, on_team=project is not None, now=now
                ),
            )
        )
    return views


async def formation_participation(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.Formation | None:
    if not event.formation_enabled:
        return None
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        return None
    settings = await load_settings(session)
    min_size = event.team_size_min or settings.team_size_default_min
    max_size = event.team_size_max or settings.team_size_default_max
    if min_size > max_size:
        max_size = min_size
    if max_size < 2:
        max_size = 2
    target = formation.target_override or max_size
    target = min(max(target, min_size), max_size)
    viable = min(max_size, max(min_size, ceil(target * settings.viable_ratio)))
    core_max = min(max_size, max(2, ceil(target * settings.core_ratio)))
    timing = formation_timing(event, settings, now)
    counts = {
        status: 0
        for status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE)
    }
    volunteers = 0
    for status, count, willing in await session.execute(
        select(
            models.Enrollment.status,
            func.count(),
            func.count().filter(models.Enrollment.willing_to_lead),
        )
        .where(
            models.Enrollment.event_id == event.id,
            models.Enrollment.status.in_(
                [EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE]
            ),
        )
        .group_by(models.Enrollment.status)
    ):
        counts[EnrollmentStatus(status)] = count
        volunteers += willing
    cores = (
        await session.execute(
            select(models.Project.id, func.count(models.ProjectMember.id))
            .outerjoin(
                models.ProjectMember,
                (models.ProjectMember.project_id == models.Project.id)
                & models.ProjectMember.left_at.is_(None),
            )
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.FORMING,
            )
            .group_by(models.Project.id)
            .order_by(models.Project.created_at, models.Project.id)
        )
    ).all()
    teams = (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status.is_not(None),
                models.Project.team_status.not_in([TeamStatus.DISBANDED, TeamStatus.CANCELLED]),
            )
        )
        or 0
    )
    free = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.ACTIVE,
                ~exists().where(
                    models.ProjectMember.event_id == event.id,
                    models.ProjectMember.user_id == models.Enrollment.user_id,
                    models.ProjectMember.left_at.is_(None),
                ),
            )
        )
        or 0
    )
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.OPEN:
        cores_sizes = [min(max_size, max(1, count)) for _, count in cores]
        try:
            bounds = placement_bounds(free, cores_sizes, viable, target, max_size)
        except ValueError:
            return None
        projection = schemas.FormationProjection(
            teams=bounds.teams,
            new_teams=bounds.new_teams,
            placed=bounds.placed,
            reserve=bounds.reserve,
            undersized_cores=bounds.undersized_cores,
            team_sizes=projected_team_sizes(
                bounds.placed, cores_sizes, bounds.new_teams, viable, target, max_size
            ),
            volunteers=volunteers,
        )
    else:
        team_rows = (
            await session.execute(
                select(models.Project.id, func.count(models.ProjectMember.id))
                .outerjoin(
                    models.ProjectMember,
                    (models.ProjectMember.project_id == models.Project.id)
                    & models.ProjectMember.left_at.is_(None),
                )
                .where(
                    models.Project.event_id == event.id,
                    models.Project.team_status.is_not(None),
                    models.Project.team_status.not_in(
                        [TeamStatus.DISBANDED.value, TeamStatus.CANCELLED.value]
                    ),
                )
                .group_by(models.Project.id)
                .order_by(models.Project.created_at, models.Project.id)
            )
        ).all()
        projection = schemas.FormationProjection(
            teams=len(team_rows),
            new_teams=0,
            placed=counts[EnrollmentStatus.PLACED],
            reserve=counts[EnrollmentStatus.RESERVE],
            undersized_cores=0,
            team_sizes=[count for _, count in team_rows],
            volunteers=volunteers,
        )
    fit = await session.get(models.LeadFit, (event.id, actor.id))
    membership = await live_membership(session, event.id, actor.id)
    rows = list(
        await session.scalars(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id, models.Enrollment.user_id == actor.id
            )
        )
    )
    enrollment = next(
        (row for row in rows if row.status in LIVE_ENROLLMENT_STATES),
        max(rows, key=lambda row: (row.status_at, row.id), default=None),
    )
    views = await enrollment_views(session, [enrollment], actor, now) if enrollment else []
    my_state = schemas.ParticipationState(
        enrollment=views[0] if views else None,
        team=ProjectRef(
            id=membership[1].id,
            name=membership[1].name,
            event_id=membership[1].event_id,
            team_status=TeamStatus(membership[1].team_status),
        )
        if membership
        else None,
        membership=await membership_view(session, membership[0], actor, now)
        if membership
        else None,
        lead_fit=lead_eligibility(fit, settings),
    )
    return schemas.Formation(
        event_id=event.id,
        phase=phase,
        target=target,
        viable=viable,
        min_size=min_size,
        max_size=max_size,
        core_max=core_max,
        target_override=formation.target_override,
        formation_at_override=formation.formation_at_override,
        formation_at=formation.formation_at,
        review_ends_at=formation.review_ends_at,
        team_deadline=timing.team_deadline,
        reg_deadline=timing.reg_deadline,
        rounds=formation.rounds,
        last_run_id=formation.last_run_id,
        counts=schemas.FormationCounts(
            enrollments=sum(counts.values()),
            active=counts[EnrollmentStatus.ACTIVE],
            placed=counts[EnrollmentStatus.PLACED],
            reserve=counts[EnrollmentStatus.RESERVE],
            cores=len(cores),
            core_members=sum(count for _, count in cores),
            teams=teams,
            volunteers=volunteers,
        ),
        projection=projection,
        my_state=my_state,
        allowed_actions=formation_actions(
            event,
            formation,
            actor=actor,
            enrollment=enrollment,
            on_team=membership is not None,
            lead_fit=fit,
            now=now,
            reg_deadline=timing.reg_deadline,
            settings=settings,
        ),
    )


async def formation_row(session: AsyncSession, event_id: UUID) -> models.EventFormation:
    formation = await session.get(models.EventFormation, event_id)
    if formation is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation is not initialised")
    return formation


async def resolve_preferred_area(session: AsyncSession, key: str | None) -> UUID | None:
    if key is None:
        return None
    area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
    if area_id is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Unknown area: {key}")
    return area_id


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


async def enroll(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.CreateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> tuple[models.Enrollment, bool]:
    event, phase = await participation_event(session, event_id, actor)
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    if (
        phase == FormationPhase.CLOSED
        or event.status != EventStatus.PUBLISHED
        or now >= timing.reg_deadline
        or now >= event.starts_at
    ):
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment is closed")
    if await live_membership(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event")
    area_id = await resolve_preferred_area(session, body.preferred_area_key)
    current = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if current is not None:
        if (current.preferred_area_id, current.note, current.willing_to_lead) == (
            area_id,
            body.note,
            body.willing_to_lead,
        ):
            return current, False
        raise ProblemError(ErrorCode.ALREADY_ENROLLED, "You are already enrolled in this event")
    enrollment = await latest_terminal_enrollment(session, event_id, actor.id, for_update=True)
    if enrollment is None:
        enrollment = models.Enrollment(
            event_id=event_id, user_id=actor.id, source=EnrollmentSource.SELF.value
        )
        session.add(enrollment)
    enrollment.preferred_area_id = area_id
    enrollment.note = body.note
    enrollment.willing_to_lead = body.willing_to_lead
    enrollment.status = (
        EnrollmentStatus.ACTIVE if phase == FormationPhase.OPEN else EnrollmentStatus.RESERVE
    ).value
    enrollment.status_at = now
    enrollment.reserve_rank = None
    await attendance.sync_going(session, event_id, actor.id, going=True, now=now)
    await enqueue_participation_jobs(
        session, jobs, event_id, reserve=enrollment.status == EnrollmentStatus.RESERVE.value
    )
    await session.flush()
    await session.refresh(enrollment)
    return enrollment, True


async def update_enrollment(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateEnrollmentBody,
    actor: models.User,
    now: datetime,
) -> models.Enrollment:
    event, _ = await participation_event(session, event_id, actor)
    if now >= event.starts_at:
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment edits close at the event start")
    row = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if row is None:
        raise ProblemError(ErrorCode.NOT_ENROLLED, "You are not enrolled in this event")
    changes = body.model_dump(exclude_unset=True)
    if "preferred_area_key" in changes:
        changes["preferred_area_id"] = await resolve_preferred_area(
            session, changes.pop("preferred_area_key")
        )
    if changes.get("willing_to_lead", False) is None:
        del changes["willing_to_lead"]
    for field, value in changes.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
    await session.flush()
    await session.refresh(row)
    return row


async def withdraw_enrollment(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    event, _ = await participation_event(session, event_id, actor)
    if now >= event.starts_at:
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED, "Enrollment is closed")
    if await live_membership(session, event_id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM, "Leave the team first")
    row = await live_enrollment_row(session, event_id, actor.id, for_update=True)
    if row is None:
        return None
    reserve = row.status == EnrollmentStatus.RESERVE.value
    row.status = EnrollmentStatus.WITHDRAWN.value
    row.status_at = now
    row.reserve_rank = None
    await attendance.sync_going(session, event_id, actor.id, going=False, now=now)
    await enqueue_participation_jobs(session, jobs, event_id, reserve=reserve)
    await session.flush()
    return None


async def list_enrollments(
    session: AsyncSession,
    event_id: UUID,
    actor: models.User,
    pagination: CursorParams,
    now: datetime,
) -> schemas.EnrollmentPage:
    event = await formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    statement = select(models.Enrollment).where(
        models.Enrollment.event_id == event_id,
        models.Enrollment.status.in_(sorted(member.value for member in LIVE_ENROLLMENT_STATES)),
    )
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Enrollment.created_at],
        id_column=models.Enrollment.id,
        serialize=lambda row: row,
        descending=False,
    )
    return schemas.EnrollmentPage(
        items=await enrollment_views(session, page.items, actor, now),
        next_cursor=page.next_cursor,
    )


async def list_my_enrollments(
    session: AsyncSession, actor: models.User, status: EnrollmentStatus | None, now: datetime
) -> list[schemas.Enrollment]:
    statement = select(models.Enrollment).where(models.Enrollment.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.Enrollment.status == status.value)
    statement = statement.order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
    return await enrollment_views(session, list(await session.scalars(statement)), actor, now)


async def withdraw_user_enrollments(
    session: AsyncSession, user_id: UUID, now: datetime
) -> list[UUID]:
    live = sorted(member.value for member in LIVE_ENROLLMENT_STATES)
    event_ids = list(
        await session.scalars(
            select(models.Enrollment.event_id)
            .where(models.Enrollment.user_id == user_id, models.Enrollment.status.in_(live))
            .distinct()
            .order_by(models.Enrollment.event_id)
        )
    )
    if not event_ids:
        return []
    for event_id in event_ids:
        await session.execute(
            select(models.Event).where(models.Event.id == event_id).with_for_update()
        )
    await session.execute(
        update(models.Enrollment)
        .where(
            models.Enrollment.user_id == user_id,
            models.Enrollment.event_id.in_(event_ids),
            models.Enrollment.status.in_(live),
        )
        .values(status=EnrollmentStatus.WITHDRAWN.value, status_at=now, reserve_rank=None)
    )
    for event_id in event_ids:
        await attendance.sync_going(session, event_id, user_id, going=False, now=now)
    await session.flush()
    return event_ids


def _lead_fit_events_query() -> Select[models.Event]:
    return (
        select(models.Event)
        .join(models.EventFormation, models.EventFormation.event_id == models.Event.id)
        .where(
            models.Event.status == EventStatus.PUBLISHED.value,
            models.Event.formation_enabled.is_(True),
            models.EventFormation.phase.in_(sorted(member.value for member in LEAD_FIT_PHASES)),
        )
        .order_by(models.Event.id)
    )


async def lead_fit_events(session: AsyncSession) -> list[models.Event]:
    return list(await session.scalars(_lead_fit_events_query()))


async def lead_fit_events_for_member(session: AsyncSession, user_id: UUID) -> list[models.Event]:
    teams = select(models.ProjectMember.event_id).where(
        models.ProjectMember.user_id == user_id,
        models.ProjectMember.event_id.is_not(None),
        models.ProjectMember.left_at.is_(None),
        models.ProjectMember.is_team_row.is_(True),
    )
    return list(
        await session.scalars(_lead_fit_events_query().where(models.Event.id.notin_(teams)))
    )


async def lead_fit_candidates(
    session: AsyncSession, event_id: UUID, now: datetime
) -> list[models.User]:
    team_members = select(models.ProjectMember.user_id).where(
        models.ProjectMember.event_id == event_id,
        models.ProjectMember.left_at.is_(None),
        models.ProjectMember.is_team_row.is_(True),
    )
    return list(
        await session.scalars(
            select(models.User)
            .where(
                models.User.onboarding_status.in_(sorted(ONBOARDED_STATUSES)),
                models.User.banned_at.is_(None),
                models.User.id.notin_(team_members),
                models.User.dnd_indefinite.is_(False),
                or_(models.User.dnd_until.is_(None), models.User.dnd_until <= now),
            )
            .order_by(models.User.id)
        )
    )


async def stored_lead_fit(
    session: AsyncSession, event_id: UUID, user_id: UUID
) -> models.LeadFit | None:
    return await session.scalar(
        select(models.LeadFit).where(
            models.LeadFit.event_id == event_id, models.LeadFit.user_id == user_id
        )
    )


def validated_lead_fits(user_ids: set[UUID], fits: Sequence[AlgoLeadFit]) -> list[AlgoLeadFit]:
    kept: dict[UUID, AlgoLeadFit] = {}
    for fit in fits:
        if fit.user_id not in user_ids or fit.user_id in kept:
            continue
        kept[fit.user_id] = AlgoLeadFit(
            user_id=fit.user_id, fit=min(1.0, max(0.0, fit.fit)), reason=fit.reason
        )
    return sorted(kept.values(), key=lambda fit: str(fit.user_id))


async def store_lead_fits(
    session: AsyncSession,
    event_id: UUID,
    fits: Sequence[AlgoLeadFit],
    algo_version: str,
    now: datetime,
) -> None:
    for fit in fits:
        value = Decimal(str(fit.fit)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        await session.execute(
            insert(models.LeadFit)
            .values(
                event_id=event_id,
                user_id=fit.user_id,
                fit=value,
                reason=fit.reason,
                algo_version=algo_version,
                computed_at=now,
            )
            .on_conflict_do_update(
                index_elements=[models.LeadFit.event_id, models.LeadFit.user_id],
                set_={
                    "fit": value,
                    "reason": fit.reason,
                    "algo_version": algo_version,
                    "computed_at": now,
                },
            )
        )
    return None


async def my_lead_fit(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> schemas.LeadEligibility:
    event = await formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if await session.get(models.EventFormation, event_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    fit = await stored_lead_fit(session, event_id, actor.id)
    if fit is None:
        raise ProblemError(ErrorCode.LEAD_FIT_PENDING, "Lead fit is being computed")
    return cast(schemas.LeadEligibility, lead_eligibility(fit, await load_settings(session)))


def lead_candidate_tier(
    user_id: UUID,
    fit: float,
    willing_to_lead: bool,
    lead_user_ids: set[UUID],
    settings: RuntimeSettings,
) -> int:
    if user_id in lead_user_ids:
        return 1
    if willing_to_lead and fit >= settings.lead_fit_threshold:
        return 2
    if willing_to_lead:
        return 3
    return 4


async def list_lead_candidates(
    session: AsyncSession, event_id: UUID
) -> list[schemas.LeadCandidate]:
    event = await session.get(models.Event, event_id)
    if event is None or not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if await session.get(models.EventFormation, event_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    rows = (
        await session.execute(
            select(models.LeadFit, models.User)
            .join(models.User, models.User.id == models.LeadFit.user_id)
            .where(models.LeadFit.event_id == event_id)
        )
    ).all()
    enrollments: dict[UUID, models.Enrollment] = {}
    for enrollment in await session.scalars(
        select(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.status.in_(sorted(member.value for member in LIVE_ENROLLMENT_STATES)),
        )
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
    ):
        enrollments.setdefault(enrollment.user_id, enrollment)
    lead_user_ids: set[UUID] = {
        lead_user_id
        for lead_user_id in await session.scalars(
            select(models.Project.lead_user_id).where(
                models.Project.event_id == event_id,
                models.Project.team_status.in_(
                    [
                        TeamStatus.FORMING.value,
                        TeamStatus.REVIEW.value,
                        TeamStatus.LOCKED.value,
                        TeamStatus.ACTIVE.value,
                    ]
                ),
                models.Project.lead_user_id.is_not(None),
            )
        )
        if lead_user_id is not None
    }
    settings = await load_settings(session)
    candidates: list[schemas.LeadCandidate] = []
    for fit, user in rows:
        value = float(fit.fit)
        enrollment = enrollments.get(user.id)
        willing = enrollment.willing_to_lead if enrollment else False
        candidates.append(
            schemas.LeadCandidate(
                user=UserRef(
                    id=user.id, display_name=user.display_name, avatar_url=user.avatar_url
                ),
                fit=value,
                eligible=value >= settings.lead_fit_threshold,
                reason=fit.reason,
                computed_at=fit.computed_at,
                willing_to_lead=willing,
                tier=lead_candidate_tier(user.id, value, willing, lead_user_ids, settings),
                enrolled_at=enrollment.created_at if enrollment else None,
            )
        )
    return sorted(
        candidates, key=lambda candidate: (candidate.tier, -candidate.fit, str(candidate.user.id))
    )


async def close_event_pending_items(
    session: AsyncSession, event_id: UUID, user_id: UUID, now: datetime, *, reason: str
) -> None:
    await session.execute(
        update(models.Invite)
        .where(
            models.Invite.event_id == event_id,
            models.Invite.user_id == user_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
        .values(status=InviteStatus.CLOSED.value, closed_reason=reason, responded_at=now)
    )
    await session.execute(
        update(models.JoinRequest)
        .where(
            models.JoinRequest.event_id == event_id,
            models.JoinRequest.user_id == user_id,
            models.JoinRequest.status == JoinRequestStatus.PENDING.value,
        )
        .values(status=JoinRequestStatus.CLOSED.value, responded_at=now)
    )
    return None


async def late_claim_open(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    user_id: UUID,
    now: datetime,
) -> bool:
    return False


async def claim_lead(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.CreateTeamBody,
    actor: models.User,
    now: datetime,
) -> models.Project | None:
    event = await locked_formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event_id)
    if event.status != EventStatus.PUBLISHED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Lead claims are closed")
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if phase == FormationPhase.OPEN:
        if now >= timing.reg_deadline:
            raise ProblemError(
                ErrorCode.ENROLLMENT_CLOSED, "Lead claims close at the registration deadline"
            )
    elif phase == FormationPhase.LOCKED:
        if not await late_claim_open(session, event, formation, actor.id, now):
            raise ProblemError(ErrorCode.INVALID_STATE, "Lead claims are closed")
    else:
        raise ProblemError(ErrorCode.INVALID_STATE, "Lead claims are closed")
    if await live_membership(session, event.id, actor.id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event")
    if dnd_active(actor, now):
        raise ProblemError(ErrorCode.USER_DND, "Turn off do not disturb to claim a team")
    led = (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.lead_user_id == actor.id,
                models.Project.status.notin_(
                    sorted(member.value for member in TERMINAL_PROJECT_STATUSES)
                ),
            )
        )
        or 0
    )
    if led >= settings.projects_led_max:
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    fit = await stored_lead_fit(session, event.id, actor.id)
    if fit is None:
        await defer_job(session, jobs, JobName.COMPUTE_LEAD_FITS_FOR_MEMBER, user_id=str(actor.id))
        return None
    if float(fit.fit) < settings.lead_fit_threshold:
        raise ProblemError(ErrorCode.LEAD_FIT_TOO_LOW, fit.reason)
    project = models.Project(
        event_id=event.id,
        name=body.name,
        description_md=body.description_md or "",
        goal="",
        links=[],
        visibility=ProjectVisibility.PUBLIC.value,
        recruiting=None,
        lead_user_id=actor.id,
        status=ProjectStatus.IDEA.value,
        team_status=TeamStatus.FORMING.value,
        origin=ProjectOrigin.CLAIM.value,
        ready_at=None,
        formed_in_run_id=None,
        last_lead_action_at=now,
    )
    session.add(project)
    await session.flush()
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=event.id,
            user_id=actor.id,
            role=MemberRole.LEAD.value,
            area_id=None,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=JoinedVia.CLAIM.value,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=True,
            joined_at=now,
            confirmed_at=now,
        )
    )
    enrollment = await live_enrollment_row(session, event.id, actor.id, for_update=True)
    if enrollment is None:
        enrollment = await latest_terminal_enrollment(session, event.id, actor.id, for_update=True)
    if enrollment is None:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=actor.id,
            status=EnrollmentStatus.PLACED.value,
            source=EnrollmentSource.SELF.value,
            willing_to_lead=False,
        )
        session.add(enrollment)
    enrollment.status = EnrollmentStatus.PLACED.value
    enrollment.status_at = now
    enrollment.reserve_rank = None
    await close_event_pending_items(session, event.id, actor.id, now, reason="claimed_lead")
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before=None,
        after={
            "name": project.name,
            "lead_user_id": str(actor.id),
            "origin": ProjectOrigin.CLAIM.value,
            "team_status": TeamStatus.FORMING.value,
        },
        reason=None,
    )
    await session.flush()
    await enqueue_participation_jobs(session, jobs, event.id, reserve=False)
    return project


async def team_view(
    session: AsyncSession, project: models.Project, actor: models.User, now: datetime
) -> project_schemas.Project:
    views = await projects.project_views(session, [project], actor, now)
    if not isinstance(views[0], project_schemas.Project):
        raise ProblemError(ErrorCode.INTERNAL_ERROR)
    return views[0]


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


async def maybe_early_start(
    session: AsyncSession, event: models.Event, formation: models.EventFormation, now: datetime
) -> bool:
    if FormationPhase(formation.phase) != FormationPhase.OPEN:
        return False
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    claimed = (
        models.Project.event_id == event.id,
        models.Project.team_status == TeamStatus.FORMING.value,
        models.Project.origin.in_([ProjectOrigin.CLAIM.value, ProjectOrigin.LATE_CLAIM.value]),
        models.Project.lead_user_id.is_not(None),
    )
    leads = await session.scalar(select(func.count()).select_from(models.Project).where(*claimed))
    if not leads:
        return False
    pending = await session.scalar(
        select(func.count())
        .select_from(models.Project)
        .where(*claimed, models.Project.ready_at.is_(None))
    )
    if pending:
        return False
    live_states = [EnrollmentStatus.ACTIVE.value, EnrollmentStatus.PLACED.value]
    live = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status.in_(live_states),
            )
        )
        or 0
    )
    if live < sizes.viable:
        return False
    candidate = now + timedelta(hours=1)
    if candidate >= formation.formation_at:
        return False
    previous = formation.formation_at
    formation.formation_at = candidate
    formation.review_ends_at = min(
        candidate + timedelta(hours=settings.review_hours),
        formation_timing(event, settings, now).team_deadline,
    )
    user_ids = list(
        await session.scalars(
            select(models.Enrollment.user_id).where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status.in_(live_states),
            )
        )
    )
    payload = FormationRescheduledPayload(
        link=f"/events/{event.id}/formation",
        event_id=event.id,
        event_title=event.title,
        previous_formation_at=previous,
        formation_at=candidate,
        timezone=event.timezone,
        reason="Early start: every claimed lead is ready",
    )
    await notify_many(
        session,
        user_ids,
        payload,
        now,
        dedupe_key=f"formation_rescheduled:{event.id}:{candidate.isoformat()}",
    )
    await session.flush()
    return True


async def close_project_pending_items(
    session: AsyncSession, project_id: UUID, now: datetime, *, reason: str
) -> None:
    await session.execute(
        update(models.Invite)
        .where(
            models.Invite.project_id == project_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
        .values(status=InviteStatus.CLOSED.value, closed_reason=reason, responded_at=now)
    )
    await session.execute(
        update(models.JoinRequest)
        .where(
            models.JoinRequest.project_id == project_id,
            models.JoinRequest.status == JoinRequestStatus.PENDING.value,
        )
        .values(status=JoinRequestStatus.CLOSED.value, responded_at=now)
    )
    return None


async def _locked_team(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> tuple[models.Event, models.Project]:
    project = await session.get(models.Project, project_id)
    if project is None or project.event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, project.event_id, actor)
    locked = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event, locked


async def set_ready(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime, *, ready: bool
) -> models.Project:
    event, project = await _locked_team(session, project_id, actor)
    formation = await formation_row(session, event.id)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if project.team_status != TeamStatus.FORMING.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Readiness is only available for a forming core"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if phase != FormationPhase.OPEN:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Readiness is only available while formation is open"
        )
    if ready and project.ready_at is None:
        project.ready_at = now
        project.last_lead_action_at = now
        await session.flush()
        await maybe_early_start(session, event, formation, now)
        await session.refresh(project)
    elif not ready and project.ready_at is not None:
        project.ready_at = None
        project.last_lead_action_at = now
        await session.flush()
        await session.refresh(project)
    return project


async def unclaim(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.Project:
    event, project = await _locked_team(session, project_id, actor)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if project.team_status != TeamStatus.FORMING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Only a forming core can be unclaimed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    members = list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )
    for member in members:
        member.left_at = now
        member.left_reason = LeftReason.DISBANDED.value
        enrollment = await live_enrollment_row(session, event.id, member.user_id, for_update=True)
        if enrollment is not None and enrollment.status == EnrollmentStatus.PLACED.value:
            enrollment.status = EnrollmentStatus.ACTIVE.value
            enrollment.status_at = now
            enrollment.reserve_rank = None
    project.team_status = TeamStatus.DISBANDED.value
    project.status = ProjectStatus.DISBANDED.value
    project.lead_user_id = None
    project.ready_at = None
    project.last_lead_action_at = now
    await close_project_pending_items(session, project.id, now, reason="team_disbanded")
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.PROJECT_DISBAND,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"team_status": TeamStatus.FORMING.value, "lead_user_id": str(actor.id)},
        after={
            "team_status": TeamStatus.DISBANDED.value,
            "lead_user_id": None,
            "reason": "unclaimed",
        },
        reason=None,
    )
    others = [member.user_id for member in members if member.user_id != actor.id]
    if others:
        payload = TeamDisbandedPayload(
            link=f"/events/{event.id}/formation",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            reason="The lead unclaimed the team",
        )
        await notify_many(session, others, payload, now, dedupe_key=f"team_disbanded:{project.id}")
    await session.flush()
    await enqueue_participation_jobs(session, jobs, event.id, reserve=False)
    await session.refresh(project)
    return project
