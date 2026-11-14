from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import (
    AlgoSettings,
    Core,
    EnrolledMember,
    FormationPlan,
    FormationRequest,
    MemberSnapshot,
    PlannedMember,
    PlannedMerge,
    PlannedTeam,
    RankedMember,
    ReserveRequest,
    TeamComposition,
    TeamMemberSnapshot,
)
from hmm_contracts.algo import LeadFit as AlgoLeadFit
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AreaKey,
    AuditAction,
    AuditEntityType,
    ConfirmKind,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventKind,
    EventStatus,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    MergeProposedBy,
    OnboardingStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.formation import placement_bounds
from hmm_contracts.notifications import (
    ActionKey,
    AlertKind,
    ApiAction,
    FormationReadyPayload,
    FormationRescheduledPayload,
    InviteAcceptedPayload,
    InviteClosedPayload,
    InviteDeclinedPayload,
    InviteReceivedPayload,
    JoinRequestAcceptedPayload,
    JoinRequestReceivedPayload,
    JoinRequestRejectedPayload,
    LeadAssignedPayload,
    LeadOfferDeclinedPayload,
    LeadOfferPayload,
    LeadSteppedDownPayload,
    LeadTransferredPayload,
    MergeProposedPayload,
    NotificationPerson,
    ProjectInviteReceivedPayload,
    ProjectJoinRequestReceivedPayload,
    ReservePlacedPayload,
    TeamDisbandedPayload,
    TeamMemberLeftPayload,
    TeamProposedPayload,
    TeamReconfirmPayload,
)
from pydantic import JsonValue
from sqlalchemy import Select, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import service as attendance
from hmm_core.formation import schemas
from hmm_core.notifications.service import notify, notify_many, raise_alert
from hmm_core.platform.algo import call_algo
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, Runtime, defer_job
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import AreaScoreSummary, EventRef, ProjectRef, ReasonBody, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects
from hmm_core.scores.snapshot import build_member_snapshots

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
    settings: AlgoSettings,
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


def notification_person(user: models.User) -> NotificationPerson:
    return NotificationPerson(user_id=user.id, display_name=user.display_name)


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


async def invite_context(
    session: AsyncSession, invite_id: UUID, actor: models.User
) -> tuple[models.Invite, models.Project, models.Event | None, models.EventFormation | None]:
    invite = await session.get(models.Invite, invite_id)
    if invite is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, invite.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    if project.team_status is not None and project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        formation = await formation_row(session, event.id)
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == invite.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    invite = await session.scalar(
        select(models.Invite)
        .where(models.Invite.id == invite_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invite is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return invite, project, event, formation


async def invite_views(
    session: AsyncSession, invites: Sequence[models.Invite], actor: models.User
) -> list[project_schemas.Invite]:
    if not invites:
        return []
    project_ids = {invite.project_id for invite in invites}
    user_ids = {invite.user_id for invite in invites} | {
        invite.invited_by for invite in invites if invite.invited_by is not None
    }
    area_ids = {invite.area_id for invite in invites if invite.area_id is not None}
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(models.Project.id.in_(project_ids))
        )
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    area_keys: dict[UUID, str] = (
        {
            area.id: area.key
            for area in await session.scalars(
                select(models.Area).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    return [
        projects.invite_view(
            projects_by_id[invite.project_id],
            invite,
            users=users,
            area_keys=area_keys,
            actor=actor,
        )
        for invite in invites
    ]


async def late_core_open(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    project: models.Project,
    now: datetime,
) -> bool:
    return False


async def create_invite(
    session: AsyncSession,
    project_id: UUID,
    body: project_schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> models.Invite:
    project, event = await invite_project(session, project_id, actor, lock=True)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    invitee = await session.get(models.User, body.user_id)
    if invitee is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if invitee.onboarding_status not in ONBOARDED_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Invitee is not an onboarded member")
    if invitee.banned_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Invitee is banned")
    if dnd_active(invitee, now):
        raise ProblemError(ErrorCode.USER_DND, "This member has do-not-disturb enabled")
    settings = await load_settings(session)
    area_id = await resolve_preferred_area(session, body.area_key)
    existing = await session.scalar(
        select(models.Invite.id).where(
            models.Invite.project_id == project.id,
            models.Invite.user_id == body.user_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
    )
    if existing is not None:
        raise ProblemError(ErrorCode.ALREADY_INVITED, "This member already has a pending invite")
    ttl = timedelta(days=settings.invite_ttl_days)
    if event is not None:
        formation = await formation_row(session, event.id)
        phase = FormationPhase(formation.phase)
        sizes = formation_sizes(event, formation, settings)
        if await live_membership(session, event.id, body.user_id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "The invitee is already on a team for this event"
            )
        live_count = (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
            or 0
        )
        if phase == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status == TeamStatus.FORMING.value:
            if phase != FormationPhase.OPEN and not await late_core_open(
                session, event, formation, project, now
            ):
                raise ProblemError(ErrorCode.INVALID_STATE, "Core invites are closed")
            if phase == FormationPhase.OPEN and now >= formation.formation_at:
                raise ProblemError(
                    ErrorCode.INVALID_STATE, "Core invites close at the formation time"
                )
            kind = InviteKind.CORE.value
            pending = (
                await session.scalar(
                    select(func.count())
                    .select_from(models.Invite)
                    .where(
                        models.Invite.project_id == project.id,
                        models.Invite.kind == InviteKind.CORE.value,
                        models.Invite.status == InviteStatus.PENDING.value,
                    )
                )
                or 0
            )
            if pending >= sizes.core_max - live_count:
                raise ProblemError(
                    ErrorCode.CORE_FULL,
                    f"Core is full: {live_count} of {sizes.core_max} members"
                    f" with {pending} pending invites",
                )
            expires_at = min(now + ttl, formation.formation_at)
        elif project.team_status in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value):
            timing = formation_timing(event, settings, now)
            if now >= timing.reg_deadline:
                raise ProblemError(
                    ErrorCode.ENROLLMENT_CLOSED, "Fill invites close at the registration deadline"
                )
            if live_count >= sizes.max_size:
                raise ProblemError(
                    ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}"
                )
            kind = InviteKind.FILL.value
            expires_at = min(now + ttl, timing.reg_deadline)
        else:
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is not accepting invites")
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == body.user_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The invitee is already a member of this project"
            )
        kind = InviteKind.PROJECT.value
        expires_at = now + ttl
    invite = models.Invite(
        project_id=project.id,
        event_id=event.id if event is not None else None,
        user_id=body.user_id,
        invited_by=actor.id,
        kind=kind,
        message=body.message,
        area_id=area_id,
        status=InviteStatus.PENDING.value,
        closed_reason=None,
        expires_at=expires_at,
        responded_at=None,
    )
    session.add(invite)
    await session.flush()
    project.last_lead_action_at = now
    await session.flush()
    actions = (
        ApiAction(key=ActionKey.INVITE_ACCEPT, entity_id=invite.id, label_key=ACCEPT_LABEL),
        ApiAction(key=ActionKey.INVITE_DECLINE, entity_id=invite.id, label_key=DECLINE_LABEL),
    )
    if event is not None:
        await notify(
            session,
            invitee.id,
            InviteReceivedPayload(
                link=f"/projects/{project.id}",
                actions=actions,
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                invite_id=invite.id,
                kind=InviteKind.CORE if kind == InviteKind.CORE.value else InviteKind.FILL,
                actor=notification_person(actor),
                invitee=notification_person(invitee),
                expires_at=expires_at,
                message=body.message,
                area_key=body.area_key,
            ),
            now,
            dedupe_key=f"invite_received:{invite.id}",
        )
    else:
        await notify(
            session,
            invitee.id,
            ProjectInviteReceivedPayload(
                link=f"/projects/{project.id}",
                actions=actions,
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                actor=notification_person(actor),
                expires_at=expires_at,
                message=body.message,
                area_key=body.area_key,
            ),
            now,
            dedupe_key=f"project_invite_received:{invite.id}",
        )
    return invite


async def list_invites(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[project_schemas.Invite]:
    project, _ = await invite_project(session, project_id, actor, lock=False)
    if not actor.is_admin and actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    invites = await session.scalars(
        select(models.Invite)
        .where(models.Invite.project_id == project.id)
        .order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
    )
    return await invite_views(session, list(invites), actor)


async def list_my_invites(
    session: AsyncSession, status: InviteStatus | None, actor: models.User
) -> list[project_schemas.Invite]:
    statement = select(models.Invite).where(models.Invite.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.Invite.status == status.value)
    statement = statement.order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
    return await invite_views(session, list(await session.scalars(statement)), actor)


async def close_invitee_items(
    session: AsyncSession,
    event: models.Event,
    invitee: models.User,
    now: datetime,
    *,
    closed_reason: str,
    notified_reason: str,
    keep: UUID | None,
) -> None:
    statement = select(models.Invite).where(
        models.Invite.event_id == event.id,
        models.Invite.user_id == invitee.id,
        models.Invite.status == InviteStatus.PENDING.value,
    )
    if keep is not None:
        statement = statement.where(models.Invite.id != keep)
    closing = list(await session.scalars(statement))
    await close_event_pending_items(session, event.id, invitee.id, now, reason=closed_reason)
    if not closing:
        return None
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({invite.project_id for invite in closing})
            )
        )
    }
    for invite in closing:
        project = projects_by_id[invite.project_id]
        recipient = project.lead_user_id or invite.invited_by
        if recipient is None:
            continue
        await notify(
            session,
            recipient,
            InviteClosedPayload(
                link=f"/projects/{invite.project_id}",
                project_id=invite.project_id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(invitee),
                invitee=notification_person(invitee),
                expires_at=invite.expires_at,
                event_id=event.id,
                event_title=event.title,
                reason=notified_reason,
            ),
            now,
            dedupe_key=f"invite_closed:{invite.id}",
        )
    return None


async def accept_invite(
    session: AsyncSession,
    jobs: procrastinate.App,
    invite_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.Invite:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if actor.id != invite.user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.ACCEPTED.value:
        return invite
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if now >= invite.expires_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite has expired")
    settings = await load_settings(session)
    live_count_query = (
        select(func.count())
        .select_from(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status not in (
            TeamStatus.FORMING.value,
            TeamStatus.REVIEW.value,
            TeamStatus.LOCKED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is no longer accepting members")
        if await live_membership(session, event.id, actor.id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event"
            )
        sizes = formation_sizes(event, formation, settings)
        live_count = await session.scalar(live_count_query) or 0
        if live_count >= sizes.max_size:
            raise ProblemError(ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}")
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "You are already a member of this project")
        member_count = await session.scalar(live_count_query) or 0
        if member_count >= settings.project_size_max:
            raise ProblemError(
                ErrorCode.PROJECT_FULL, f"Project has reached max size {settings.project_size_max}"
            )
    invite.status = InviteStatus.ACCEPTED.value
    invite.responded_at = now
    joined_via = {
        InviteKind.CORE.value: JoinedVia.CORE_INVITE.value,
        InviteKind.FILL.value: JoinedVia.FILL_INVITE.value,
    }.get(invite.kind, JoinedVia.INVITE.value)
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=event.id if event is not None else None,
            user_id=actor.id,
            role=MemberRole.MEMBER.value,
            area_id=invite.area_id,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=joined_via,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=event is not None,
            joined_at=now,
            confirmed_at=now,
        )
    )
    reserve = False
    if event is not None:
        enrollment = await live_enrollment_row(session, event.id, actor.id, for_update=True)
        if enrollment is None:
            enrollment = await latest_terminal_enrollment(
                session, event.id, actor.id, for_update=True
            )
        if enrollment is None:
            enrollment = models.Enrollment(
                event_id=event.id,
                user_id=actor.id,
                source=EnrollmentSource.INVITE.value,
                willing_to_lead=False,
            )
            session.add(enrollment)
        else:
            reserve = enrollment.status == EnrollmentStatus.RESERVE.value
        enrollment.status = EnrollmentStatus.PLACED.value
        enrollment.status_at = now
        enrollment.reserve_rank = None
    await session.flush()
    if event is not None:
        await close_invitee_items(
            session,
            event,
            actor,
            now,
            closed_reason="joined_team",
            notified_reason="The invitee joined another team",
            keep=invite.id,
        )
    recipient = project.lead_user_id or invite.invited_by
    if recipient is not None:
        await notify(
            session,
            recipient,
            InviteAcceptedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(actor),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"invite_accepted:{invite.id}",
        )
    if event is not None:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=reserve)
    await session.flush()
    await session.refresh(invite)
    return invite


async def decline_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> models.Invite:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if actor.id != invite.user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.DECLINED.value:
        return invite
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if now >= invite.expires_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite has expired")
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status not in (
            TeamStatus.FORMING.value,
            TeamStatus.REVIEW.value,
            TeamStatus.LOCKED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This team is no longer accepting members")
    elif project.status in (
        ProjectStatus.ABANDONED.value,
        ProjectStatus.DISBANDED.value,
        ProjectStatus.CANCELLED.value,
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    invite.status = InviteStatus.DECLINED.value
    invite.responded_at = now
    recipient = project.lead_user_id or invite.invited_by
    if recipient is not None:
        await notify(
            session,
            recipient,
            InviteDeclinedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(actor),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"invite_declined:{invite.id}",
        )
    await session.flush()
    await session.refresh(invite)
    return invite


async def withdraw_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> None:
    invite, project, event, formation = await invite_context(session, invite_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if invite.status == InviteStatus.WITHDRAWN.value:
        return None
    if invite.status != InviteStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This invite is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    invite.status = InviteStatus.WITHDRAWN.value
    invite.responded_at = now
    project.last_lead_action_at = now
    invitee = await session.get(models.User, invite.user_id)
    if invitee is not None:
        await notify(
            session,
            invite.user_id,
            InviteClosedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=invite.id,
                kind=InviteKind(invite.kind),
                actor=notification_person(actor),
                invitee=notification_person(invitee),
                expires_at=invite.expires_at,
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
                reason="The lead withdrew the invite",
            ),
            now,
            dedupe_key=f"invite_closed:{invite.id}",
        )
    await session.flush()
    return None


async def join_request_context(
    session: AsyncSession, join_request_id: UUID, actor: models.User
) -> tuple[models.JoinRequest, models.Project, models.Event | None, models.EventFormation | None]:
    request = await session.get(models.JoinRequest, join_request_id)
    if request is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, request.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    if project.team_status is not None and project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        formation = await formation_row(session, event.id)
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == request.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    request = await session.scalar(
        select(models.JoinRequest)
        .where(models.JoinRequest.id == join_request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return request, project, event, formation


async def join_request_views(
    session: AsyncSession, requests: Sequence[models.JoinRequest], actor: models.User
) -> list[project_schemas.JoinRequest]:
    if not requests:
        return []
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({request.project_id for request in requests})
            )
        )
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_({request.user_id for request in requests}))
        )
    }
    return [
        projects.join_request_view(
            projects_by_id[request.project_id], request, users=users, actor=actor
        )
        for request in requests
    ]


async def create_join_request(
    session: AsyncSession,
    project_id: UUID,
    body: project_schemas.CreateProjectJoinRequestBody,
    actor: models.User,
    now: datetime,
) -> models.JoinRequest:
    project, event = await invite_project(session, project_id, actor, lock=True)
    if event is not None:
        formation = await formation_row(session, event.id)
        phase = FormationPhase(formation.phase)
        if phase == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status != TeamStatus.FORMING.value:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Join requests are only accepted by a forming core"
            )
        if project.lead_user_id is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This team has no lead")
        if await live_membership(session, event.id, actor.id) is not None:
            raise ProblemError(
                ErrorCode.ALREADY_IN_TEAM, "You are already on a team for this event"
            )
        enrollment = await live_enrollment_row(session, event.id, actor.id, for_update=True)
        if enrollment is None:
            raise ProblemError(
                ErrorCode.NOT_ENROLLED, "Enroll in the hackathon before requesting to join a core"
            )
        late = await late_core_open(session, event, formation, project, now)
        if enrollment.status == EnrollmentStatus.RESERVE.value and not late:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "An active enrollment is required")
        if not late and now >= formation.formation_at:
            raise ProblemError(ErrorCode.INVALID_STATE, "Join requests close at the formation time")
        duplicate = await session.scalar(
            select(models.JoinRequest.id).where(
                models.JoinRequest.event_id == event.id,
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
        )
        if duplicate is not None:
            raise ProblemError(
                ErrorCode.JOIN_REQUEST_PENDING,
                "You already have a pending join request for this hackathon",
            )
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        if project.recruiting is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is not recruiting")
        if project.lead_user_id is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "This project has no lead")
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "You are already a member of this project")
        duplicate = await session.scalar(
            select(models.JoinRequest.id).where(
                models.JoinRequest.project_id == project.id,
                models.JoinRequest.event_id.is_(None),
                models.JoinRequest.user_id == actor.id,
                models.JoinRequest.status == JoinRequestStatus.PENDING.value,
            )
        )
        if duplicate is not None:
            raise ProblemError(
                ErrorCode.JOIN_REQUEST_PENDING,
                "You already have a pending join request for this project",
            )
    request = models.JoinRequest(
        project_id=project.id,
        event_id=event.id if event is not None else None,
        user_id=actor.id,
        note=body.note,
        status=JoinRequestStatus.PENDING.value,
        responded_at=None,
    )
    session.add(request)
    await session.flush()
    lead = (
        await session.get(models.User, project.lead_user_id)
        if project.lead_user_id is not None
        else None
    )
    if lead is not None:
        actions = (
            ApiAction(
                key=ActionKey.JOIN_REQUEST_ACCEPT, entity_id=request.id, label_key=ACCEPT_LABEL
            ),
            ApiAction(
                key=ActionKey.JOIN_REQUEST_REJECT, entity_id=request.id, label_key=REJECT_LABEL
            ),
        )
        if event is not None:
            await notify(
                session,
                lead.id,
                JoinRequestReceivedPayload(
                    link=f"/projects/{project.id}",
                    actions=actions,
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    join_request_id=request.id,
                    requester=notification_person(actor),
                    lead=notification_person(lead),
                    note=body.note,
                ),
                now,
                dedupe_key=f"join_request_received:{request.id}",
            )
        else:
            await notify(
                session,
                lead.id,
                ProjectJoinRequestReceivedPayload(
                    link=f"/projects/{project.id}",
                    actions=actions,
                    project_id=project.id,
                    project_name=project.name,
                    join_request_id=request.id,
                    requester=notification_person(actor),
                    note=body.note,
                ),
                now,
                dedupe_key=f"project_join_request_received:{request.id}",
            )
    return request


async def list_join_requests(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[project_schemas.JoinRequest]:
    project, _ = await invite_project(session, project_id, actor, lock=False)
    if not actor.is_admin and actor.id != project.lead_user_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    requests = await session.scalars(
        select(models.JoinRequest)
        .where(models.JoinRequest.project_id == project.id)
        .order_by(models.JoinRequest.created_at.desc(), models.JoinRequest.id.desc())
    )
    return await join_request_views(session, list(requests), actor)


async def withdraw_join_request(
    session: AsyncSession, join_request_id: UUID, actor: models.User, now: datetime
) -> None:
    request, _, _, formation = await join_request_context(session, join_request_id, actor)
    if request.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.WITHDRAWN.value:
        return None
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    request.status = JoinRequestStatus.WITHDRAWN.value
    request.responded_at = now
    await session.flush()
    return None


async def accept_join_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    join_request_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.JoinRequest:
    request, project, event, formation = await join_request_context(session, join_request_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.ACCEPTED.value:
        return request
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    settings = await load_settings(session)
    live_count_query = (
        select(func.count())
        .select_from(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    enrollment: models.Enrollment | None = None
    if event is not None and formation is not None:
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
        if project.team_status != TeamStatus.FORMING.value:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Join requests are only accepted by a forming core"
            )
        requester = await session.get(models.User, request.user_id)
        if requester is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if await live_membership(session, event.id, request.user_id) is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The requester is already on a team for this event"
            )
        enrollment = await live_enrollment_row(session, event.id, request.user_id, for_update=True)
        if enrollment is None:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "The requester is no longer enrolled")
        late = await late_core_open(session, event, formation, project, now)
        if enrollment.status == EnrollmentStatus.RESERVE.value and not late:
            raise ProblemError(ErrorCode.NOT_ENROLLED, "An active enrollment is required")
        if not late and now >= formation.formation_at:
            raise ProblemError(ErrorCode.INVALID_STATE, "Join requests close at the formation time")
        sizes = formation_sizes(event, formation, settings)
        live_count = await session.scalar(live_count_query) or 0
        if live_count >= sizes.max_size:
            raise ProblemError(ErrorCode.TEAM_FULL, f"Team has reached max size {sizes.max_size}")
    else:
        if project.status in (
            ProjectStatus.ABANDONED.value,
            ProjectStatus.DISBANDED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
        requester = await session.get(models.User, request.user_id)
        if requester is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        member = await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == requester.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is not None:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "The requester is already a member of this project"
            )
        member_count = await session.scalar(live_count_query) or 0
        if member_count >= settings.project_size_max:
            raise ProblemError(
                ErrorCode.PROJECT_FULL, f"Project has reached max size {settings.project_size_max}"
            )
    request.status = JoinRequestStatus.ACCEPTED.value
    request.responded_at = now
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=event.id if event is not None else None,
            user_id=request.user_id,
            role=MemberRole.MEMBER.value,
            area_id=None,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=JoinedVia.JOIN_REQUEST.value,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=event is not None,
            joined_at=now,
            confirmed_at=now,
        )
    )
    reserve = False
    if enrollment is not None:
        reserve = enrollment.status == EnrollmentStatus.RESERVE.value
        enrollment.status = EnrollmentStatus.PLACED.value
        enrollment.status_at = now
        enrollment.reserve_rank = None
    await session.flush()
    if event is not None:
        await close_invitee_items(
            session,
            event,
            requester,
            now,
            closed_reason="joined_team",
            notified_reason="The invitee joined another team",
            keep=None,
        )
    project.last_lead_action_at = now
    await notify(
        session,
        requester.id,
        JoinRequestAcceptedPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            join_request_id=request.id,
            requester=notification_person(requester),
            lead=notification_person(actor),
            event_id=event.id if event is not None else None,
            event_title=event.title if event is not None else None,
        ),
        now,
        dedupe_key=f"join_request_accepted:{request.id}",
    )
    if event is not None:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=reserve)
    await session.flush()
    await session.refresh(request)
    return request


async def reject_join_request(
    session: AsyncSession, join_request_id: UUID, actor: models.User, now: datetime
) -> models.JoinRequest:
    request, project, event, formation = await join_request_context(session, join_request_id, actor)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == JoinRequestStatus.REJECTED.value:
        return request
    if request.status != JoinRequestStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This join request is no longer pending")
    if formation is not None and FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    request.status = JoinRequestStatus.REJECTED.value
    request.responded_at = now
    project.last_lead_action_at = now
    requester = await session.get(models.User, request.user_id)
    if requester is not None:
        await notify(
            session,
            requester.id,
            JoinRequestRejectedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                join_request_id=request.id,
                requester=notification_person(requester),
                lead=notification_person(actor),
                event_id=event.id if event is not None else None,
                event_title=event.title if event is not None else None,
            ),
            now,
            dedupe_key=f"join_request_rejected:{request.id}",
        )
    await session.flush()
    await session.refresh(request)
    return request


async def withdraw_user_pending_items(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    await session.execute(
        update(models.Invite)
        .where(
            models.Invite.user_id == user_id,
            models.Invite.status == InviteStatus.PENDING.value,
        )
        .values(status=InviteStatus.WITHDRAWN.value, responded_at=now)
    )
    await session.execute(
        update(models.JoinRequest)
        .where(
            models.JoinRequest.user_id == user_id,
            models.JoinRequest.status == JoinRequestStatus.PENDING.value,
        )
        .values(status=JoinRequestStatus.WITHDRAWN.value, responded_at=now)
    )
    return None


@dataclass(frozen=True, slots=True)
class LeadOrderEntry:
    member: models.ProjectMember
    user: models.User
    fit: float
    tier: int
    enrolled_at: datetime | None


async def lead_order(
    session: AsyncSession, project: models.Project, now: datetime
) -> list[LeadOrderEntry]:
    if project.event_id is None:
        return []
    rows = (
        await session.execute(
            select(models.ProjectMember, models.User)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.User.banned_at.is_(None),
            )
        )
    ).all()
    if not rows:
        return []
    user_ids = {user.id for _, user in rows}
    enrollments: dict[UUID, models.Enrollment] = {
        enrollment.user_id: enrollment
        for enrollment in await session.scalars(
            select(models.Enrollment).where(
                models.Enrollment.event_id == project.event_id,
                models.Enrollment.user_id.in_(user_ids),
                models.Enrollment.status.in_(
                    sorted(member.value for member in LIVE_ENROLLMENT_STATES)
                ),
            )
        )
    }
    fits: dict[UUID, models.LeadFit] = {
        row.user_id: row
        for row in await session.scalars(
            select(models.LeadFit).where(
                models.LeadFit.event_id == project.event_id,
                models.LeadFit.user_id.in_(user_ids),
            )
        )
    }
    claimed = {project.lead_user_id} if project.lead_user_id is not None else set()
    settings = await load_settings(session)
    entries: list[LeadOrderEntry] = []
    for member, user in rows:
        row = fits.get(user.id)
        fit = float(row.fit) if row is not None else 0.0
        enrollment = enrollments.get(user.id)
        entries.append(
            LeadOrderEntry(
                member=member,
                user=user,
                fit=fit,
                tier=lead_candidate_tier(
                    user.id,
                    fit,
                    enrollment.willing_to_lead if enrollment else False,
                    claimed,
                    settings,
                ),
                enrolled_at=enrollment.created_at if enrollment else None,
            )
        )
    return sorted(
        entries,
        key=lambda entry: (
            entry.tier,
            -entry.fit,
            entry.enrolled_at is None,
            entry.enrolled_at or now,
            str(entry.user.id),
        ),
    )


async def team_member_ids(session: AsyncSession, project_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def lead_offer_expiry(
    session: AsyncSession, project: models.Project, event: models.Event | None, now: datetime
) -> datetime:
    settings = await load_settings(session)
    if event is None or project.event_id is None:
        return now + projects.STANDALONE_TRANSFER_TTL
    formation = await session.get(models.EventFormation, event.id)
    if project.team_status == TeamStatus.FORMING.value:
        cap = formation.formation_at if formation is not None else event.starts_at
    elif project.team_status == TeamStatus.REVIEW.value:
        cap = (
            project.review_ends_at
            or (formation.review_ends_at if formation is not None else None)
            or event.starts_at
        )
    elif project.team_status == TeamStatus.LOCKED.value:
        cap = event.starts_at
    elif project.team_status == TeamStatus.ACTIVE.value:
        cap = event.ends_at
    elif project.team_status in RESULT_TEAM_STATUSES:
        return now + projects.STANDALONE_TRANSFER_TTL
    else:
        raise ProblemError(ErrorCode.INVALID_STATE, "This team cannot receive lead offers")
    return min(now + timedelta(hours=settings.lead_offer_hours), cap)


async def close_pending_lead_offers(session: AsyncSession, project_id: UUID, now: datetime) -> None:
    await session.execute(
        update(models.LeadOffer)
        .where(
            models.LeadOffer.project_id == project_id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            models.LeadOffer.source == LeadOfferSource.TRANSFER.value,
        )
        .values(status=LeadOfferStatus.CANCELLED.value, responded_at=now)
    )
    await session.execute(
        update(models.LeadOffer)
        .where(
            models.LeadOffer.project_id == project_id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            models.LeadOffer.source != LeadOfferSource.TRANSFER.value,
        )
        .values(status=LeadOfferStatus.CLOSED.value, responded_at=now)
    )
    return None


async def create_lead_offer(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    user_id: UUID,
    now: datetime,
    *,
    source: LeadOfferSource,
    reason: str,
    offered_by: UUID | None = None,
    tier: int | None = None,
) -> models.LeadOffer:
    user = await session.get(models.User, user_id)
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    member = await session.scalar(
        select(models.ProjectMember.id)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
        .limit(1)
    )
    if member is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "The offered member is not on this team")
    await close_pending_lead_offers(session, project.id, now)
    expires_at = await lead_offer_expiry(session, project, event, now)
    offer = models.LeadOffer(
        project_id=project.id,
        event_id=project.event_id,
        user_id=user_id,
        source=source.value,
        offered_by=offered_by,
        tier=tier,
        reason=reason,
        status=LeadOfferStatus.PENDING.value,
        expires_at=expires_at,
    )
    session.add(offer)
    await session.flush()
    if event is not None:
        offerer = await session.get(models.User, offered_by) if offered_by is not None else None
        await notify(
            session,
            user_id,
            LeadOfferPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                lead_offer_id=offer.id,
                source=source,
                offered_member=notification_person(user),
                offered_by=notification_person(offerer) if offerer is not None else None,
                expires_at=expires_at,
            ),
            now,
            dedupe_key=f"lead_offer:{offer.id}",
        )
    return offer


async def lead_offer_responses(
    session: AsyncSession, offers: Sequence[models.LeadOffer], actor: models.User
) -> list[project_schemas.LeadOffer]:
    if not offers:
        return []
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({offer.project_id for offer in offers})
            )
        )
    }
    user_ids = {offer.user_id for offer in offers} | {
        offer.offered_by for offer in offers if offer.offered_by is not None
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    views: list[project_schemas.LeadOffer] = []
    for offer in offers:
        project = projects_by_id.get(offer.project_id)
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        views.append(projects.lead_offer_view(project, offer, users=users, actor=actor))
    return views


async def lead_offer_response(
    session: AsyncSession, offer: models.LeadOffer, actor: models.User
) -> project_schemas.LeadOffer:
    return (await lead_offer_responses(session, [offer], actor))[0]


async def start_succession(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    exclude: set[UUID],
    reason: str,
) -> models.LeadOffer | None:
    if event is None:
        await projects.standalone_lead_succession(session, project, now, exclude=set(exclude))
        return None
    project.lead_succession_excluded = sorted(set(exclude), key=str)
    return await _offer_next(session, project, event, now, reason=reason)


async def advance_succession(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    exclude: UUID,
    reason: str,
) -> models.LeadOffer | None:
    if event is None:
        await projects.standalone_lead_succession(session, project, now, exclude={exclude})
        return None
    project.lead_succession_excluded = sorted({*project.lead_succession_excluded, exclude}, key=str)
    return await _offer_next(session, project, event, now, reason=reason)


async def _offer_next(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    now: datetime,
    *,
    reason: str,
) -> models.LeadOffer | None:
    if project.lead_user_id is not None:
        return None
    excluded = set(project.lead_succession_excluded)
    entries = [
        entry for entry in await lead_order(session, project, now) if entry.user.id not in excluded
    ]
    if not entries:
        return await _exhausted(session, project, event, now)
    expires_at = await lead_offer_expiry(session, project, event, now)
    if expires_at <= now:
        return await _exhausted(session, project, event, now)
    return await create_lead_offer(
        session,
        project,
        event,
        entries[0].user.id,
        now,
        source=LeadOfferSource.SUCCESSION,
        reason=reason,
        tier=entries[0].tier,
    )


async def _exhausted(
    session: AsyncSession, project: models.Project, event: models.Event | None, now: datetime
) -> None:
    if project.team_status in FORCEABLE_TEAM_STATUSES and event is not None:
        await force_lead(session, project, event, now, reason="No member accepted the lead role")
    return None


async def force_lead(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    reason: str,
) -> models.ProjectMember | None:
    if event is None or project.lead_user_id is not None:
        return None
    if project.team_status not in FORCEABLE_TEAM_STATUSES:
        return None
    entry = next(
        (
            entry
            for entry in await lead_order(session, project, now)
            if entry.member.state == MemberState.CONFIRMED.value
        ),
        None,
    )
    if entry is None:
        return None
    await close_pending_lead_offers(session, project.id, now)
    project.lead_user_id = entry.user.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    entry.member.role = MemberRole.LEAD.value
    await session.flush()
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": None},
        after={"lead_user_id": str(entry.user.id), "forced": True},
        reason=reason,
    )
    await notify_many(
        session,
        await team_member_ids(session, project.id),
        LeadAssignedPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            lead=notification_person(entry.user),
            reason=reason,
        ),
        now,
        dedupe_key=f"lead_assigned:{project.id}:{entry.user.id}",
    )
    await raise_alert(
        session,
        AlertKind.FORCED_LEAD,
        now,
        title=f"Lead role forced: {project.name}",
        message=f"{entry.user.display_name} was assigned as lead because nobody accepted the offer",
        link=f"/admin/projects/{project.id}",
        subject_type="project",
        subject_id=project.id,
        subject_title=project.name,
        dedupe_key=f"forced_lead:{project.id}",
    )
    return entry.member


async def locked_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User
) -> tuple[models.LeadOffer, models.Project, models.Event | None]:
    offer = await session.get(models.LeadOffer, offer_id)
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, offer.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = (
        await locked_formation_event(session, project.event_id, actor)
        if project.event_id is not None
        else None
    )
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == offer.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    offer = await session.scalar(
        select(models.LeadOffer)
        .where(models.LeadOffer.id == offer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return offer, project, event


async def _retire_lead_offer(
    session: AsyncSession,
    offer: models.LeadOffer,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    status: LeadOfferStatus,
    reason: str,
) -> None:
    offer.status = status.value
    offer.responded_at = now
    if offer.source != LeadOfferSource.TRANSFER.value:
        await advance_succession(session, project, event, now, exclude=offer.user_id, reason=reason)
    await session.flush()
    return None


async def expire_lead_offer(
    session: AsyncSession,
    offer: models.LeadOffer,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
) -> None:
    await _retire_lead_offer(
        session,
        offer,
        project,
        event,
        now,
        status=LeadOfferStatus.EXPIRED,
        reason="The lead offer expired",
    )
    return None


async def my_lead_offers(
    session: AsyncSession, actor: models.User, status: LeadOfferStatus | None
) -> list[project_schemas.LeadOffer]:
    statement = select(models.LeadOffer).where(models.LeadOffer.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.LeadOffer.status == status.value)
    statement = statement.order_by(models.LeadOffer.created_at.desc(), models.LeadOffer.id.desc())
    return await lead_offer_responses(session, list(await session.scalars(statement)), actor)


async def _persisted_problem(session: AsyncSession, detail: str) -> ProblemError:
    await session.flush()
    return ProblemError(ErrorCode.INVALID_STATE, detail)


async def accept_lead_offer(
    session: AsyncSession,
    jobs: procrastinate.App,
    offer_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.LeadOffer | ProblemError:
    offer, project, event = await locked_lead_offer(session, offer_id, actor)
    if offer.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if offer.status == LeadOfferStatus.ACCEPTED.value:
        return offer
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    if offer.expires_at <= now:
        await expire_lead_offer(session, offer, project, event, now)
        return await _persisted_problem(session, "This lead offer has expired")
    transfer = offer.source == LeadOfferSource.TRANSFER.value
    if project.lead_user_id is not None and not (
        transfer and project.lead_user_id == offer.offered_by
    ):
        offer.status = LeadOfferStatus.CLOSED.value
        offer.responded_at = now
        return await _persisted_problem(session, "This team already has a lead")
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "You are no longer on this team")
    previous_lead_id = project.lead_user_id
    if previous_lead_id is not None:
        previous = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == previous_lead_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if previous is not None:
            previous.role = MemberRole.MEMBER.value
            await session.flush()
    member.role = MemberRole.LEAD.value
    member.state = MemberState.CONFIRMED.value
    member.confirm_by = None
    member.confirm_kind = None
    member.confirmed_at = member.confirmed_at or now
    offer.status = LeadOfferStatus.ACCEPTED.value
    offer.responded_at = now
    project.lead_user_id = actor.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    await session.flush()
    if transfer:
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_TRANSFER,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(offer.offered_by)},
            after={"lead_user_id": str(actor.id)},
            reason=None,
        )
    else:
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_ASSIGN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": None},
            after={"lead_user_id": str(actor.id), "source": offer.source},
            reason=None,
        )
    if event is not None:
        recipients = await team_member_ids(session, project.id)
        previous_user = (
            await session.get(models.User, previous_lead_id)
            if transfer and previous_lead_id is not None
            else None
        )
        if previous_user is not None:
            await notify_many(
                session,
                recipients,
                LeadTransferredPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    previous_lead=notification_person(previous_user),
                    lead=notification_person(actor),
                ),
                now,
                dedupe_key=f"lead_transferred:{offer.id}",
            )
        else:
            await notify_many(
                session,
                recipients,
                LeadAssignedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    lead=notification_person(actor),
                    reason="The member accepted the lead offer",
                ),
                now,
                dedupe_key=f"lead_assigned:{project.id}:{actor.id}",
            )
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
    await session.flush()
    await session.refresh(offer)
    return offer


async def decline_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User, now: datetime
) -> models.LeadOffer | ProblemError:
    offer, project, event = await locked_lead_offer(session, offer_id, actor)
    if offer.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if offer.status == LeadOfferStatus.DECLINED.value:
        return offer
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    if offer.expires_at <= now:
        await expire_lead_offer(session, offer, project, event, now)
        return await _persisted_problem(session, "This lead offer has expired")
    offer.status = LeadOfferStatus.DECLINED.value
    offer.responded_at = now
    await session.flush()
    if offer.source == LeadOfferSource.TRANSFER.value:
        if offer.offered_by is not None and event is not None:
            await notify(
                session,
                offer.offered_by,
                LeadOfferDeclinedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    lead_offer_id=offer.id,
                    offered_member=notification_person(actor),
                    reason="The member declined the lead role",
                ),
                now,
                dedupe_key=f"lead_offer_declined:{offer.id}",
            )
    elif project.lead_user_id is None:
        await advance_succession(
            session,
            project,
            event,
            now,
            exclude=offer.user_id,
            reason="The next candidate in the lead order",
        )
    await session.flush()
    await session.refresh(offer)
    return offer


async def cancel_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User, now: datetime
) -> None:
    offer, project, _ = await locked_lead_offer(session, offer_id, actor)
    if offer.offered_by != actor.id or project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if offer.source != LeadOfferSource.TRANSFER.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Only a transfer offer can be cancelled")
    if offer.status == LeadOfferStatus.CANCELLED.value:
        return None
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    offer.status = LeadOfferStatus.CANCELLED.value
    offer.responded_at = now
    project.last_lead_action_at = now
    await session.flush()
    return None


async def open_seat(
    session: AsyncSession, project: models.Project, event: models.Event, now: datetime
) -> None:
    return None


async def _locked_lead_project(
    session: AsyncSession, project_id: UUID, actor: models.User, *, phase_gate: bool
) -> tuple[models.Project, models.Event | None]:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    if project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        formation = await formation_row(session, event.id)
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if (
        formation is not None
        and (phase_gate or project.team_status not in RESULT_TEAM_STATUSES)
        and FormationPhase(formation.phase) == FormationPhase.FORMING
    ):
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    return project, event


async def transfer_lead(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.TransferLeadBody,
    actor: models.User,
    now: datetime,
) -> models.LeadOffer:
    project, event = await _locked_lead_project(session, project_id, actor, phase_gate=True)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if body.user_id == actor.id:
        raise ProblemError(ErrorCode.INVALID_STATE, "You are already the lead")
    target = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == body.user_id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if target is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "The target is not on this team")
    if target.state != MemberState.CONFIRMED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Only a confirmed member can take the lead")
    pending = await session.scalar(
        select(models.LeadOffer.id).where(
            models.LeadOffer.project_id == project.id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
        )
    )
    if pending is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "A lead offer is already pending for this team")
    tier = (
        next(
            (
                entry.tier
                for entry in await lead_order(session, project, now)
                if entry.user.id == target.user_id
            ),
            None,
        )
        if event is not None
        else None
    )
    offer = await create_lead_offer(
        session,
        project,
        event,
        target.user_id,
        now,
        source=LeadOfferSource.TRANSFER,
        reason=f"Lead transfer offered by {actor.display_name}",
        offered_by=actor.id,
        tier=tier,
    )
    if event is not None:
        fit = await stored_lead_fit(session, event.id, target.user_id)
        target_user = await session.get_one(models.User, target.user_id)
        if fit is None or float(fit.fit) < (await load_settings(session)).lead_fit_threshold:
            await raise_alert(
                session,
                AlertKind.LEAD_BELOW_THRESHOLD,
                now,
                title=f"Lead transfer below threshold: {project.name}",
                message=f"{target_user.display_name} has a lead fit below the threshold",
                link=f"/admin/projects/{project.id}",
                subject_type="project",
                subject_id=project.id,
                subject_title=project.name,
                dedupe_key=f"lead_below_threshold:{project.id}:{target.user_id}",
            )
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(offer)
    return offer


async def _notify_lead_stepped_down(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    previous: models.User,
    now: datetime,
) -> None:
    recipients = [
        user_id for user_id in await team_member_ids(session, project.id) if user_id != previous.id
    ]
    await notify_many(
        session,
        recipients,
        LeadSteppedDownPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            previous_lead=notification_person(previous),
        ),
        now,
        dedupe_key=f"lead_stepped_down:{project.id}:{previous.id}",
    )
    return None


async def step_down(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.Project:
    project, event = await _locked_lead_project(session, project_id, actor, phase_gate=True)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "You are not on this team")
    await close_pending_lead_offers(session, project.id, now)
    project.lead_user_id = None
    project.last_lead_action_at = now
    member.role = MemberRole.MEMBER.value
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.LEAD_STEP_DOWN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": str(actor.id)},
        after={"lead_user_id": None},
        reason=None,
    )
    if event is not None:
        await _notify_lead_stepped_down(session, project, event, actor, now)
    await start_succession(
        session, project, event, now, exclude={actor.id}, reason="The lead stepped down"
    )
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
    await session.flush()
    await session.refresh(project)
    return project


async def _release_lead(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    user_id: UUID,
    now: datetime,
    *,
    notice_reason: str,
    audit_actor: UUID | None,
    audit_kind: ActorKind | None,
) -> None:
    project.lead_user_id = None
    project.last_lead_action_at = now
    await close_pending_lead_offers(session, project.id, now)
    if audit_kind is not None:
        await record_audit(
            session,
            actor=audit_actor,
            actor_kind=audit_kind,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(user_id)},
            after={"lead_user_id": None},
            reason=notice_reason,
        )
    previous = await session.get(models.User, user_id)
    if previous is not None:
        await _notify_lead_stepped_down(session, project, event, previous, now)
    await start_succession(
        session, project, event, now, exclude={user_id}, reason="The lead left the team"
    )
    return None


async def remove_member_from_team(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    member: models.ProjectMember,
    now: datetime,
    *,
    left_reason: LeftReason,
    notice_reason: str,
    audit_actor: UUID | None,
    audit_kind: ActorKind | None,
) -> EnrollmentStatus | None:
    was_lead = project.lead_user_id == member.user_id
    member.left_at = now
    member.left_reason = left_reason.value
    result: EnrollmentStatus | None = None
    row = await live_enrollment_row(session, event.id, member.user_id, for_update=True)
    if row is not None:
        result = EnrollmentStatus(row.status)
        if row.status == EnrollmentStatus.PLACED.value:
            phase = FormationPhase((await formation_row(session, event.id)).phase)
            if phase == FormationPhase.OPEN:
                result = EnrollmentStatus.ACTIVE
            elif phase in (FormationPhase.REVIEW, FormationPhase.LOCKED):
                result = EnrollmentStatus.RESERVE
                row.reserve_rank = None
            else:
                result = EnrollmentStatus.WITHDRAWN
            row.status = result.value
            row.status_at = now
    await open_seat(session, project, event, now)
    await session.flush()
    remaining = await team_member_ids(session, project.id)
    if was_lead:
        await _release_lead(
            session,
            project,
            event,
            member.user_id,
            now,
            notice_reason=notice_reason,
            audit_actor=audit_actor,
            audit_kind=audit_kind,
        )
    if (
        FormationPhase((await formation_row(session, event.id)).phase) == FormationPhase.OPEN
        and project.team_status == TeamStatus.FORMING.value
        and not remaining
    ):
        project.team_status = TeamStatus.DISBANDED.value
        project.status = ProjectStatus.DISBANDED.value
        project.lead_user_id = None
        project.ready_at = None
        await close_project_pending_items(session, project.id, now, reason="team_disbanded")
        await record_audit(
            session,
            actor=audit_actor,
            actor_kind=audit_kind or ActorKind.SYSTEM,
            action=AuditAction.PROJECT_DISBAND,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"team_status": TeamStatus.FORMING.value},
            after={"team_status": TeamStatus.DISBANDED.value, "reason": "empty_core"},
            reason=notice_reason,
        )
    user = await session.get(models.User, member.user_id)
    if user is not None and remaining:
        await notify_many(
            session,
            remaining,
            TeamMemberLeftPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                member=notification_person(user),
                member_count=len(remaining),
                reason=notice_reason,
            ),
            now,
            dedupe_key=f"team_member_left:{project.id}:{member.user_id}",
        )
    await session.flush()
    return result


async def leave_team(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.Project:
    initial = await session.get(models.Project, project_id)
    if initial is None or initial.event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project, event = await _locked_lead_project(session, project_id, actor, phase_gate=False)
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status in projects.LEAD_CLOSED_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "You can no longer leave this team")
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.team_status in RESULT_TEAM_STATUSES:
        was_lead = project.lead_user_id == actor.id
        member.left_at = now
        member.left_reason = LeftReason.LEFT.value
        await session.flush()
        if was_lead:
            await _release_lead(
                session,
                project,
                event,
                actor.id,
                now,
                notice_reason="The member left the team",
                audit_actor=actor.id,
                audit_kind=ActorKind.USER,
            )
        await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
        await session.flush()
        await session.refresh(project)
        return project
    result = await remove_member_from_team(
        session,
        project,
        event,
        member,
        now,
        left_reason=LeftReason.LEFT,
        notice_reason="The member left the team",
        audit_actor=actor.id,
        audit_kind=ActorKind.USER,
    )
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
    if result == EnrollmentStatus.RESERVE:
        await defer_job(session, jobs, JobName.RANK_RESERVE, event_id=str(event.id))
    await session.flush()
    await session.refresh(project)
    return project


async def expire_lead_offers(session: AsyncSession, event: models.Event, now: datetime) -> int:
    await session.execute(
        select(models.Event.id).where(models.Event.id == event.id).with_for_update()
    )
    due = (
        await session.execute(
            select(models.LeadOffer.id, models.LeadOffer.project_id)
            .where(
                models.LeadOffer.event_id == event.id,
                models.LeadOffer.status == LeadOfferStatus.PENDING.value,
                models.LeadOffer.expires_at <= now,
            )
            .order_by(models.LeadOffer.created_at, models.LeadOffer.id)
        )
    ).all()
    if not due:
        return 0
    locked = {
        project.id: project
        for project in await session.scalars(
            select(models.Project)
            .where(models.Project.id.in_({project_id for _, project_id in due}))
            .order_by(models.Project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    expired = 0
    for offer_id, _ in due:
        offer = await session.get(
            models.LeadOffer, offer_id, with_for_update=True, populate_existing=True
        )
        if offer is None or offer.status != LeadOfferStatus.PENDING.value:
            continue
        project = locked[offer.project_id]
        if projects.lead_changes_closed(project):
            offer.status = LeadOfferStatus.EXPIRED.value
            offer.responded_at = now
        else:
            await expire_lead_offer(session, offer, project, event, now)
        expired += 1
    await session.flush()
    return expired


async def remove_user_from_teams(
    session: AsyncSession, user_id: UUID, now: datetime, *, actor: UUID
) -> list[UUID]:
    rows = (
        await session.execute(
            select(models.Project, models.ProjectMember)
            .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
            .where(
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.Project.team_status.is_not(None),
            )
            .order_by(models.Project.id)
        )
    ).all()
    offered = (
        await session.execute(
            select(models.LeadOffer.id, models.Project.id, models.Project.event_id)
            .join(models.Project, models.Project.id == models.LeadOffer.project_id)
            .where(
                models.LeadOffer.user_id == user_id,
                models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            )
            .order_by(models.LeadOffer.id)
        )
    ).all()
    if not rows and not offered:
        return []
    removable = [
        (project, member)
        for project, member in rows
        if project.team_status in REMOVABLE_TEAM_STATUSES and project.event_id is not None
    ]
    kept = [project for project, _ in rows if project.team_status == TeamStatus.ACTIVE.value]
    events: dict[UUID, models.Event] = {}
    locked: dict[UUID, models.Project] = {}
    project_ids = sorted(
        {project.id for project, _ in removable} | {project_id for _, project_id, _ in offered}
    )
    event_ids = sorted(
        {cast(UUID, project.event_id) for project, _ in removable}
        | {event_id for _, _, event_id in offered if event_id is not None}
    )
    if event_ids:
        await session.execute(
            select(models.Event.id)
            .where(models.Event.id.in_(event_ids))
            .order_by(models.Event.id)
            .with_for_update()
        )
        for event_id in event_ids:
            events[event_id] = await session.get_one(models.Event, event_id)
    if project_ids:
        locked = {
            project.id: project
            for project in await session.scalars(
                select(models.Project)
                .where(models.Project.id.in_(project_ids))
                .order_by(models.Project.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
    offers = (
        list(
            await session.scalars(
                select(models.LeadOffer)
                .where(models.LeadOffer.id.in_([offer_id for offer_id, _, _ in offered]))
                .order_by(models.LeadOffer.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if offered
        else []
    )
    settings = await load_settings(session)
    for stale, member in removable:
        project = locked[stale.id]
        current = await session.scalar(
            select(models.ProjectMember)
            .where(models.ProjectMember.id == member.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None or current.left_at is not None:
            continue
        event = events[cast(UUID, project.event_id)]
        min_size = event.team_size_min or settings.team_size_default_min
        await remove_member_from_team(
            session,
            project,
            event,
            current,
            now,
            left_reason=LeftReason.BANNED,
            notice_reason="The member left the team",
            audit_actor=actor,
            audit_kind=ActorKind.ADMIN,
        )
        size = len(await team_member_ids(session, project.id))
        if project.team_status in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value) and (
            size < min_size
        ):
            await raise_alert(
                session,
                AlertKind.TEAM_BELOW_MIN,
                now,
                title=f"Team below minimum: {project.name}",
                message=(
                    f"{project.name} has {size} members after a member was banned"
                    f" (minimum {min_size})"
                ),
                link=f"/admin/projects/{project.id}",
                subject_type="project",
                subject_id=project.id,
                subject_title=project.name,
                dedupe_key=f"team_below_min:{project.id}",
            )
    if kept:
        user = await session.get(models.User, user_id)
        if user is not None:
            for project in kept:
                await raise_alert(
                    session,
                    AlertKind.BANNED_MEMBER_IN_ACTIVE_TEAM,
                    now,
                    title=f"Banned member on an active team: {project.name}",
                    message=(
                        f"{user.display_name} was banned while on the active team {project.name}"
                    ),
                    link=f"/admin/projects/{project.id}",
                    subject_type="project",
                    subject_id=project.id,
                    subject_title=project.name,
                    dedupe_key=f"banned_member_in_active_team:{project.id}:{user_id}",
                )
    for offer in offers:
        await session.refresh(offer)
        project = locked[offer.project_id]
        if offer.status != LeadOfferStatus.PENDING.value or projects.lead_changes_closed(project):
            continue
        await _retire_lead_offer(
            session,
            offer,
            project,
            events.get(project.event_id) if project.event_id is not None else None,
            now,
            status=LeadOfferStatus.CLOSED,
            reason="The offered member is no longer available",
        )
    await session.flush()
    return sorted(project.id for project, _ in removable)


async def preserved_projects(
    session: AsyncSession, event_id: UUID, kind: FormationKind, phase: FormationPhase
) -> list[models.Project]:
    if kind == FormationKind.LATE:
        scope = (
            models.Project.team_status == TeamStatus.FORMING.value,
            models.Project.origin == ProjectOrigin.LATE_CLAIM.value,
        )
    elif phase == FormationPhase.OPEN:
        scope = (models.Project.team_status == TeamStatus.FORMING.value,)
    elif phase == FormationPhase.REVIEW:
        scope = (models.Project.team_status == TeamStatus.REVIEW.value,)
    else:
        return []
    return list(
        await session.scalars(
            select(models.Project)
            .where(models.Project.event_id == event_id, *scope)
            .order_by(models.Project.created_at, models.Project.id)
        )
    )


@dataclass(frozen=True, slots=True)
class MemberFacts:
    willing_to_lead: bool
    enrolled_at: datetime
    fit: float
    tier: int


def formation_member_facts(
    request: FormationRequest, settings: AlgoSettings
) -> dict[UUID, MemberFacts]:
    members: list[TeamMemberSnapshot | EnrolledMember] = [
        snapshot for core in request.cores for snapshot in (core.lead, *core.members)
    ]
    members.extend(request.unplaced)
    facts: dict[UUID, MemberFacts] = {}
    for member in members:
        if member.user_id in facts:
            continue
        lead_fit = request.lead_fits.get(member.user_id)
        fit = lead_fit.fit if lead_fit is not None else 0.0
        facts[member.user_id] = MemberFacts(
            willing_to_lead=member.willing_to_lead,
            enrolled_at=member.enrolled_at,
            fit=fit,
            tier=lead_candidate_tier(member.user_id, fit, member.willing_to_lead, set(), settings),
        )
    return facts


def planned_lead_user(facts: dict[UUID, MemberFacts], user_ids: Sequence[UUID]) -> UUID | None:
    if not user_ids:
        return None
    return min(
        user_ids,
        key=lambda uid: (facts[uid].tier, -facts[uid].fit, facts[uid].enrolled_at, str(uid)),
    )


def _free_pool_statuses(kind: FormationKind, phase: FormationPhase) -> frozenset[str]:
    if kind == FormationKind.LATE:
        return frozenset({EnrollmentStatus.RESERVE.value})
    if phase == FormationPhase.OPEN:
        return frozenset({EnrollmentStatus.ACTIVE.value})
    if phase == FormationPhase.REVIEW:
        return frozenset({EnrollmentStatus.ACTIVE.value, EnrollmentStatus.RESERVE.value})
    return frozenset()


async def build_formation_request(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    kind: FormationKind,
    phase: FormationPhase,
    now: datetime,
) -> FormationRequest:
    from hmm_core.catalog import service as catalog

    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    preserved = await preserved_projects(session, event.id, kind, phase)
    project_ids = [project.id for project in preserved]
    rows: list[models.ProjectMember] = (
        list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id.in_(project_ids),
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )
        if project_ids
        else []
    )
    enrollments: dict[UUID, models.Enrollment] = {
        enrollment.user_id: enrollment
        for enrollment in await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status.in_(
                    sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                ),
            )
            .order_by(models.Enrollment.created_at, models.Enrollment.id)
        )
    }
    user_ids = {row.user_id for row in rows} | set(enrollments)
    users: dict[UUID, models.User] = (
        {
            user.id: user
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_(user_ids))
            )
        }
        if user_ids
        else {}
    )
    area_ids = {row.area_id for row in rows if row.area_id is not None} | {
        enrollment.preferred_area_id
        for enrollment in enrollments.values()
        if enrollment.preferred_area_id is not None
    }
    areas: dict[UUID, str] = (
        {
            area_id: key
            for area_id, key in await session.execute(
                select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )

    def area_key(area_id: UUID | None) -> str | None:
        return areas.get(area_id) if area_id is not None else None

    members_by_project: dict[UUID, list[models.ProjectMember]] = {}
    for row in rows:
        user = users.get(row.user_id)
        if user is not None and user.banned_at is None:
            members_by_project.setdefault(row.project_id, []).append(row)
    core_user_ids = {row.user_id for members in members_by_project.values() for row in members}
    statuses = _free_pool_statuses(kind, phase)
    free = sorted(
        (
            enrollment
            for user_id, enrollment in enrollments.items()
            if enrollment.status in statuses
            and user_id in users
            and users[user_id].banned_at is None
            and user_id not in core_user_ids
        ),
        key=lambda enrollment: (enrollment.created_at, str(enrollment.user_id)),
    )
    snapshots = await build_member_snapshots(
        session, sorted(core_user_ids | {enrollment.user_id for enrollment in free}), now
    )

    def team_member(project: models.Project, member: models.ProjectMember) -> TeamMemberSnapshot:
        enrollment = enrollments.get(member.user_id)
        return TeamMemberSnapshot(
            **snapshots[member.user_id].model_dump(),
            role=MemberRole(member.role),
            area=area_key(member.area_id),
            state=MemberState(member.state),
            willing_to_lead=enrollment.willing_to_lead if enrollment is not None else False,
            claimed_lead=member.user_id == project.lead_user_id,
            enrolled_at=enrollment.created_at if enrollment is not None else member.joined_at,
        )

    cores: list[Core] = []
    for project in preserved:
        kept = [row for row in members_by_project.get(project.id, []) if row.user_id in snapshots]
        if not kept:
            continue
        kept_ids = {row.user_id for row in kept}
        if project.lead_user_id is not None and project.lead_user_id in kept_ids:
            lead_id = project.lead_user_id
        else:
            order = await lead_order(session, project, now)
            lead_id = next(
                (entry.user.id for entry in order if entry.user.id in kept_ids), kept[0].user_id
            )
        lead_row = next(row for row in kept if row.user_id == lead_id)
        cores.append(
            Core(
                project_id=project.id,
                lead=team_member(project, lead_row),
                members=[team_member(project, row) for row in kept if row.user_id != lead_id],
            )
        )
    unplaced = [
        EnrolledMember(
            **snapshots[enrollment.user_id].model_dump(),
            preferred_area=area_key(enrollment.preferred_area_id),
            willing_to_lead=enrollment.willing_to_lead,
            enrolled_at=enrollment.created_at,
            note=enrollment.note or "",
        )
        for enrollment in free
        if enrollment.user_id in snapshots
    ]
    try:
        bounds = placement_bounds(
            len(unplaced),
            [1 + len(core.members) for core in cores],
            sizes.viable,
            sizes.target,
            sizes.max_size,
        )
    except ValueError as error:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation sizes cannot accommodate the current cores"
        ) from error
    unplaced_ids = [member.user_id for member in unplaced]
    lead_fits: dict[UUID, AlgoLeadFit] = (
        {
            row.user_id: AlgoLeadFit(user_id=row.user_id, fit=float(row.fit), reason=row.reason)
            for row in await session.scalars(
                select(models.LeadFit).where(
                    models.LeadFit.event_id == event.id,
                    models.LeadFit.user_id.in_(unplaced_ids),
                )
            )
        }
        if unplaced_ids
        else {}
    )
    return FormationRequest(
        hackathon=await catalog.event_snapshot(session, event.id),
        kind=kind,
        target=sizes.target,
        viable=sizes.viable,
        max_size=sizes.max_size,
        bounds=bounds,
        cores=cores,
        unplaced=unplaced,
        lead_fits=lead_fits,
    )


def _violation(
    constraint: int,
    message: str,
    *,
    user_id: UUID | None = None,
    project_id: UUID | None = None,
) -> JsonValue:
    return {
        "constraint": constraint,
        "message": message,
        "user_id": str(user_id) if user_id is not None else None,
        "project_id": str(project_id) if project_id is not None else None,
    }


def validate_plan(
    request: FormationRequest, plan: FormationPlan, settings: AlgoSettings
) -> list[JsonValue]:
    errors: list[JsonValue] = []
    expected = (
        {core.lead.user_id for core in request.cores}
        | {member.user_id for core in request.cores for member in core.members}
        | {member.user_id for member in request.unplaced}
    )
    actual = [member.user_id for team in plan.teams for member in team.members] + [
        ranked.user_id for ranked in plan.reserve
    ]
    occurrences: dict[UUID, int] = {}
    for user_id in actual:
        occurrences[user_id] = occurrences.get(user_id, 0) + 1
    for user_id in sorted(expected - occurrences.keys(), key=str):
        errors.append(_violation(1, "Input person is missing from the plan", user_id=user_id))
    for user_id, count in occurrences.items():
        if user_id not in expected:
            errors.append(_violation(1, "Plan contains an unknown person", user_id=user_id))
        for _ in range(count - 1):
            errors.append(_violation(1, "Person appears more than once", user_id=user_id))

    core_ids = {core.project_id for core in request.cores}
    by_project: dict[UUID, list[int]] = {}
    for index, team in enumerate(plan.teams):
        if team.project_id is not None:
            by_project.setdefault(team.project_id, []).append(index)
    for core in request.cores:
        indexes = by_project.get(core.project_id, [])
        if not indexes:
            errors.append(_violation(2, "Core has no plan team", project_id=core.project_id))
            continue
        if len(indexes) > 1:
            errors.append(
                _violation(2, "Core appears in more than one team", project_id=core.project_id)
            )
        planned = {member.user_id for member in plan.teams[indexes[0]].members}
        for member in (core.lead, *core.members):
            if member.user_id not in planned:
                errors.append(
                    _violation(
                        2,
                        "Core member is not on the core team",
                        user_id=member.user_id,
                        project_id=core.project_id,
                    )
                )
    for project_id in by_project.keys() - core_ids:
        errors.append(_violation(2, "Team references an unknown project", project_id=project_id))
    for team in plan.teams:
        if team.project_id in core_ids and team.lead_user_id is not None:
            errors.append(
                _violation(
                    2,
                    "Core team must not set a planned lead",
                    user_id=team.lead_user_id,
                    project_id=team.project_id,
                )
            )

    for team in plan.teams:
        if len(team.members) > request.max_size:
            errors.append(
                _violation(3, "Team exceeds the maximum size", project_id=team.project_id)
            )
        if team.project_id is None and len(team.members) < request.viable:
            errors.append(_violation(3, "New team is below the viable size"))
    undersized = [
        team
        for team in plan.teams
        if team.project_id in core_ids and len(team.members) < request.viable
    ]
    if len(undersized) > request.bounds.undersized_cores:
        errors.append(_violation(3, "Too many undersized cores"))
    sizes = {
        project_id: len(plan.teams[indexes[0]].members)
        for project_id, indexes in by_project.items()
    }
    merged_ids = {
        project_id
        for merge in plan.merges
        if merge.absorbed_team_id != merge.target_team_id
        and merge.absorbed_team_id in sizes
        and merge.target_team_id in sizes
        and sizes[merge.absorbed_team_id] + sizes[merge.target_team_id] <= request.max_size
        for project_id in (merge.absorbed_team_id, merge.target_team_id)
    }
    for core_team in undersized:
        if core_team.project_id in merged_ids:
            continue
        size = len(core_team.members)
        fitting = any(
            other is not core_team
            and other.project_id is not None
            and other.project_id not in merged_ids
            and len(other.members) + size <= request.max_size
            for other in plan.teams
        )
        if fitting:
            errors.append(
                _violation(
                    3, "Undersized core has no merge proposal", project_id=core_team.project_id
                )
            )

    bounds = request.bounds
    if len(plan.teams) != bounds.teams:
        errors.append(_violation(4, "Team count differs from the bounds"))
    if sum(1 for team in plan.teams if team.project_id is None) != bounds.new_teams:
        errors.append(_violation(4, "New team count differs from the bounds"))
    placed = sum(len(team.members) for team in plan.teams)
    free_placed = placed - sum(1 + len(core.members) for core in request.cores)
    if free_placed != bounds.placed:
        errors.append(_violation(4, "Placed count differs from the bounds"))
    if len(plan.reserve) != bounds.reserve:
        errors.append(_violation(4, "Reserve count differs from the bounds"))
    if sorted(ranked.rank for ranked in plan.reserve) != list(range(1, len(plan.reserve) + 1)):
        errors.append(_violation(4, "Reserve ranks are not 1..n"))

    facts = formation_member_facts(request, settings)
    for team in plan.teams:
        if team.project_id is not None:
            continue
        member_ids = [member.user_id for member in team.members]
        if team.lead_user_id is None:
            errors.append(_violation(5, "New team has no planned lead"))
            continue
        if team.lead_user_id not in member_ids:
            errors.append(
                _violation(5, "Planned lead is not on the team", user_id=team.lead_user_id)
            )
            continue
        known = [user_id for user_id in member_ids if user_id in facts]
        if team.lead_user_id != planned_lead_user(facts, known):
            errors.append(
                _violation(
                    5, "Planned lead does not follow the lead order", user_id=team.lead_user_id
                )
            )

    seen: set[tuple[UUID, UUID]] = set()
    for merge in plan.merges:
        absorbed, target = merge.absorbed_team_id, merge.target_team_id
        if absorbed not in sizes or target not in sizes:
            errors.append(
                _violation(6, "Merge references a team outside the plan", project_id=absorbed)
            )
        elif absorbed == target:
            errors.append(_violation(6, "Merge targets the same team", project_id=absorbed))
        elif sizes[absorbed] + sizes[target] > request.max_size:
            errors.append(
                _violation(6, "Merged team exceeds the maximum size", project_id=absorbed)
            )
        elif (absorbed, target) in seen:
            errors.append(_violation(6, "Merge pair repeats", project_id=absorbed))
        seen.add((absorbed, target))
    return errors


AREA_IMPORTANCE_RANK: dict[AreaImportance, int] = {
    AreaImportance.PRIMARY: 0,
    AreaImportance.REQUIRED: 1,
    AreaImportance.NICE: 2,
}


def area_priority(request: FormationRequest) -> dict[AreaKey, int]:
    priority: dict[AreaKey, int] = {}
    for area in sorted(
        request.hackathon.areas,
        key=lambda area: (AREA_IMPORTANCE_RANK[area.importance], area.area),
    ):
        priority.setdefault(area.area, AREA_IMPORTANCE_RANK[area.importance])
    return priority


def area_score(member: MemberSnapshot, key: AreaKey) -> float:
    score = member.area_scores.get(key)
    return score.effective if score is not None else 0.0


def best_area(request: FormationRequest, member: EnrolledMember) -> AreaKey | None:
    if member.preferred_area is not None:
        return member.preferred_area
    priority = area_priority(request)
    if not priority:
        return None
    return min(priority, key=lambda key: (-area_score(member, key), priority[key], key))


@dataclass(slots=True)
class PlanDraft:
    project_id: UUID | None
    members: list[PlannedMember]
    lead_user_id: UUID | None = None


def fallback_plan(request: FormationRequest, settings: AlgoSettings) -> FormationPlan:
    viable, max_size, bounds = request.viable, request.max_size, request.bounds
    priority = area_priority(request)
    facts = formation_member_facts(request, settings)
    cores = [
        PlanDraft(
            project_id=core.project_id,
            members=[
                PlannedMember(
                    user_id=member.user_id, area=member.area, reason="Preserved core member"
                )
                for member in (core.lead, *core.members)
            ],
        )
        for core in request.cores
    ]
    pool = sorted(request.unplaced, key=lambda member: (member.enrolled_at, str(member.user_id)))
    placed = 0

    def take(draft: PlanDraft, member: EnrolledMember, area: AreaKey | None, reason: str) -> None:
        nonlocal placed
        pool.remove(member)
        draft.members.append(PlannedMember(user_id=member.user_id, area=area, reason=reason))
        placed += 1

    deficits = sorted(
        (viable - len(draft.members), index)
        for index, draft in enumerate(cores)
        if len(draft.members) < viable
    )
    for deficit, index in deficits:
        draft = cores[index]
        for _ in range(deficit):
            if not pool or placed >= bounds.placed:
                break
            covered = {member.area for member in draft.members if member.area is not None}
            uncovered = [key for key in priority if key not in covered]
            if uncovered:
                key = uncovered[0]
                chosen = min(
                    pool,
                    key=lambda member: (
                        -area_score(member, key),
                        member.enrolled_at,
                        str(member.user_id),
                    ),
                )
                take(draft, chosen, key, "Fills a core toward the viable size")
            else:
                chosen = pool[0]
                take(
                    draft,
                    chosen,
                    best_area(request, chosen),
                    "Fills a core toward the viable size",
                )
        if not pool or placed >= bounds.placed:
            break

    new_teams: list[PlanDraft] = []
    for _ in range(bounds.new_teams):
        draft = PlanDraft(project_id=None, members=[])
        new_teams.append(draft)
        if not pool:
            continue
        volunteer = planned_lead_user(
            facts, [member.user_id for member in pool if member.willing_to_lead]
        )
        chosen = next(
            (member for member in pool if member.user_id == volunteer),
            pool[0],
        )
        take(draft, chosen, best_area(request, chosen), "Seeds a new team")

    while pool and placed < bounds.placed:
        short = [
            (len(draft.members), index, draft)
            for index, draft in enumerate(new_teams)
            if len(draft.members) < viable
        ]
        if not short:
            break
        draft = min(short, key=lambda entry: (entry[0], entry[1]))[2]
        chosen = pool[0]
        take(draft, chosen, best_area(request, chosen), "Fills a bounded team seat")

    ranked_drafts = [(0, index, draft) for index, draft in enumerate(new_teams)] + [
        (1, index, draft) for index, draft in enumerate(cores)
    ]
    while pool and placed < bounds.placed:
        open_drafts = [entry for entry in ranked_drafts if len(entry[2].members) < max_size]
        if not open_drafts:
            break
        draft = min(open_drafts, key=lambda entry: (len(entry[2].members), entry[0], entry[1]))[2]
        chosen = pool[0]
        take(draft, chosen, best_area(request, chosen), "Balances team sizes")

    for draft in new_teams:
        draft.lead_user_id = planned_lead_user(facts, [member.user_id for member in draft.members])

    reserve = [
        RankedMember(
            user_id=member.user_id,
            rank=rank,
            reason="Kept on the reserve by the fallback planner",
        )
        for rank, member in enumerate(pool, start=1)
    ]

    merges: list[PlannedMerge] = []
    used: set[UUID] = set()
    undersized = sorted(
        (len(draft.members), index)
        for index, draft in enumerate(cores)
        if len(draft.members) < viable
    )
    for _, index in undersized:
        draft = cores[index]
        if draft.project_id is None or draft.project_id in used:
            continue
        target = next(
            (
                other
                for other in cores
                if other is not draft
                and other.project_id is not None
                and other.project_id not in used
                and len(other.members) + len(draft.members) <= max_size
            ),
            None,
        )
        if target is None or target.project_id is None:
            continue
        merges.append(
            PlannedMerge(
                absorbed_team_id=draft.project_id,
                target_team_id=target.project_id,
                reason="Undersized core fits into another team without exceeding the maximum",
            )
        )
        used.update((draft.project_id, target.project_id))

    return FormationPlan(
        teams=[
            PlannedTeam(
                project_id=draft.project_id,
                lead_user_id=draft.lead_user_id,
                members=draft.members,
                reason="Preserved core" if draft.project_id is not None else "New team",
            )
            for draft in [*cores, *new_teams]
        ],
        reserve=reserve,
        merges=merges,
        summary=(
            "Deterministic fallback: cores filled to viable, volunteers seeded first, "
            "then area coverage and balanced sizes"
        ),
    )


async def apply_plan(
    session: AsyncSession,
    jobs: procrastinate.App,
    run: models.FormationRun,
    request: FormationRequest,
    plan: FormationPlan,
    attempts: list[JsonValue],
    planner: FormationPlanner,
    algo_version: str,
    now: datetime,
) -> bool:
    locked_run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_run is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    run = locked_run
    if run.status == FormationRunStatus.APPLIED.value:
        return False
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    formation = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if FormationPhase(formation.phase) != FormationPhase.FORMING:
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation is no longer running")
    settings = await load_settings(session)
    errors = validate_plan(request, plan, settings)
    if errors:
        first = cast(dict[str, JsonValue], errors[0])
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Plan failed validation: " + str(first["message"])
        )
    kind = FormationKind(run.kind)
    previous_rounds = formation.rounds
    rerun = kind == FormationKind.MAIN and previous_rounds > 0
    timing = formation_timing(event, settings, now)
    event_review = formation.review_ends_at or timing.review_ends_at
    late_review = min(
        now + timedelta(hours=settings.late_review_hours),
        timing.reg_deadline - timedelta(hours=1),
    )
    team_review = late_review if kind == FormationKind.LATE else event_review
    area_keys = sorted(
        {member.area for team in plan.teams for member in team.members if member.area is not None}
    )
    areas: dict[str, UUID] = (
        {
            key: area_id
            for area_id, key in await session.execute(
                select(models.Area.id, models.Area.key).where(models.Area.key.in_(area_keys))
            )
        }
        if area_keys
        else {}
    )
    placed_ids: set[UUID] = set()
    project_ids: list[UUID] = []
    for index, team in enumerate(plan.teams, start=1):
        if team.project_id is not None:
            project = await session.scalar(
                select(models.Project)
                .where(models.Project.id == team.project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if project is None:
                raise ProblemError(ErrorCode.NOT_FOUND)
            was_review = project.team_status == TeamStatus.REVIEW.value
            confirm_by = project.review_ends_at or team_review
            project.team_status = TeamStatus.REVIEW.value
            project.review_ends_at = (
                project.review_ends_at
                if was_review and project.review_ends_at is not None
                else team_review
            )
        else:
            project = models.Project(
                event_id=event.id,
                name=f"Team {run.round}-{index}",
                description_md="",
                goal="",
                links=[],
                visibility=ProjectVisibility.PUBLIC.value,
                recruiting=None,
                lead_user_id=None,
                status=ProjectStatus.IDEA.value,
                team_status=TeamStatus.REVIEW.value,
                origin=ProjectOrigin.FORMATION.value,
                ready_at=None,
                formed_in_run_id=run.id,
                review_ends_at=team_review,
                last_lead_action_at=None,
            )
            session.add(project)
            await session.flush()
            confirm_by = project.review_ends_at or team_review
        project_ids.append(project.id)
        rows: dict[UUID, models.ProjectMember] = {
            row.user_id: row
            for row in await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
            )
        }
        for member in team.members:
            placed_ids.add(member.user_id)
            row = rows.get(member.user_id)
            if row is not None:
                if rerun and row.state == MemberState.CONFIRMED.value:
                    continue
                row.state = MemberState.PROPOSED.value
                row.confirm_kind = ConfirmKind.RECONFIRM.value
                row.confirm_by = confirm_by
                row.proposal_reason = None
                continue
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=member.user_id,
                    role=MemberRole.MEMBER.value,
                    area_id=areas.get(member.area) if member.area is not None else None,
                    state=MemberState.PROPOSED.value,
                    trust=Trust.SELF.value,
                    joined_via=JoinedVia.FORMATION.value,
                    proposal_reason=member.reason,
                    confirm_by=confirm_by,
                    confirm_kind=ConfirmKind.PLACEMENT.value,
                    is_team_row=True,
                    joined_at=now,
                    confirmed_at=None,
                )
            )
        await session.flush()
        if team.project_id is None and team.lead_user_id is not None:
            entries = await lead_order(session, project, now)
            tier = next(
                (entry.tier for entry in entries if entry.user.id == team.lead_user_id),
                entries[0].tier if entries else None,
            )
            await create_lead_offer(
                session,
                project,
                event,
                team.lead_user_id,
                now,
                source=LeadOfferSource.FORMATION,
                reason="Planned lead of the new team",
                tier=tier,
            )
    live_states = sorted(status.value for status in LIVE_ENROLLMENT_STATES)
    if placed_ids:
        await session.execute(
            update(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.user_id.in_(sorted(placed_ids)),
                models.Enrollment.status.in_(live_states),
            )
            .values(status=EnrollmentStatus.PLACED.value, status_at=now, reserve_rank=None)
        )
    for ranked in plan.reserve:
        await session.execute(
            update(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.user_id == ranked.user_id,
                models.Enrollment.status.in_(live_states),
            )
            .values(status=EnrollmentStatus.RESERVE.value, status_at=now, reserve_rank=ranked.rank)
        )
    for merge in plan.merges:
        session.add(
            models.MergeProposal(
                event_id=event.id,
                run_id=run.id,
                absorbed_team_id=merge.absorbed_team_id,
                target_team_id=merge.target_team_id,
                proposed_by=MergeProposedBy.SYSTEM.value,
                status=MergeProposalStatus.PENDING.value,
            )
        )
    formation.last_run_id = run.id
    formation.rounds = run.round
    if kind == FormationKind.LATE or not plan.teams:
        formation.phase = FormationPhase.LOCKED.value
    else:
        formation.phase = FormationPhase.REVIEW.value
        formation.review_ends_at = event_review
    run.status = FormationRunStatus.APPLIED.value
    run.output = plan.model_dump(mode="json")
    run.validation_errors = attempts
    run.planner = planner.value
    run.algo_version = algo_version
    run.applied_at = now
    await session.flush()
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.FORMATION,
        entity_id=event.id,
        before={"phase": FormationPhase.FORMING.value, "rounds": previous_rounds},
        after={
            "run_id": str(run.id),
            "round": run.round,
            "planner": planner.value,
            "phase": formation.phase,
            "teams": len(plan.teams),
            "placed": len(placed_ids),
            "reserve": len(plan.reserve),
        },
        reason=None,
    )
    await enqueue_participation_jobs(session, jobs, event.id, reserve=bool(plan.reserve))
    await notify_formation(session, event, formation, run, request, plan, project_ids, now)
    return True


async def _live_team_rows(session: AsyncSession, project_id: UUID) -> list[models.ProjectMember]:
    return list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def _users_by_id(session: AsyncSession, user_ids: set[UUID]) -> dict[UUID, models.User]:
    if not user_ids:
        return {}
    return {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }


async def notify_formation(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    run: models.FormationRun,
    request: FormationRequest,
    plan: FormationPlan,
    project_ids: Sequence[UUID],
    now: datetime,
) -> None:
    rerun = run.kind == FormationKind.MAIN.value and run.round > 1
    existing = {(core.project_id, core.lead.user_id) for core in request.cores} | {
        (core.project_id, member.user_id) for core in request.cores for member in core.members
    }
    for index, team in enumerate(plan.teams):
        project = await session.get(models.Project, project_ids[index])
        if project is None:
            continue
        rows = await _live_team_rows(session, project.id)
        users = await _users_by_id(session, {row.user_id for row in rows})
        people = tuple(notification_person(users[row.user_id]) for row in rows)
        proposed_count = sum(1 for row in rows if row.state == MemberState.PROPOSED.value)
        confirmed_count = len(rows) - proposed_count
        area_ids = {row.area_id for row in rows if row.area_id is not None}
        area_keys: dict[UUID, str] = (
            {
                area_id: key
                for area_id, key in await session.execute(
                    select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
                )
            }
            if area_ids
            else {}
        )
        planned = {member.user_id for member in team.members}
        for row in rows:
            if row.user_id not in planned or row.confirm_by is None:
                continue
            if rerun and row.state == MemberState.CONFIRMED.value:
                continue
            confirm_by = row.confirm_by
            link = f"/projects/{project.id}"
            if (project.id, row.user_id) in existing:
                if row.user_id == project.lead_user_id:
                    await notify(
                        session,
                        row.user_id,
                        FormationReadyPayload(
                            link=link,
                            event_id=event.id,
                            event_title=event.title,
                            project_id=project.id,
                            team_name=project.name,
                            project_member_id=row.id,
                            confirm_by=confirm_by,
                            timezone=event.timezone,
                            members=people,
                            proposed_count=proposed_count,
                            confirmed_count=confirmed_count,
                        ),
                        now,
                        dedupe_key=f"formation_ready:{row.id}:{run.id}:{row.user_id}",
                    )
                else:
                    await notify(
                        session,
                        row.user_id,
                        TeamReconfirmPayload(
                            link=link,
                            event_id=event.id,
                            event_title=event.title,
                            project_id=project.id,
                            team_name=project.name,
                            project_member_id=row.id,
                            confirm_by=confirm_by,
                            timezone=event.timezone,
                            members=people,
                        ),
                        now,
                        dedupe_key=f"team_reconfirm:{row.id}:{run.id}:{row.user_id}",
                    )
                continue
            await notify(
                session,
                row.user_id,
                TeamProposedPayload(
                    link=link,
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    project_member_id=row.id,
                    confirm_by=confirm_by,
                    timezone=event.timezone,
                    members=people,
                    proposal_reason=row.proposal_reason or "",
                    area_key=area_keys.get(row.area_id) if row.area_id is not None else None,
                ),
                now,
                dedupe_key=f"team_proposed:{row.id}:{run.id}:{row.user_id}",
            )
    for ranked in plan.reserve:
        await notify(
            session,
            ranked.user_id,
            ReservePlacedPayload(
                link=f"/events/{event.id}/formation",
                event_id=event.id,
                event_title=event.title,
                rank=ranked.rank,
                reserve_count=len(plan.reserve),
            ),
            now,
            dedupe_key=f"reserve_placed:{run.id}:{ranked.user_id}",
        )
    sizes = {team.project_id: len(team.members) for team in plan.teams if team.project_id}
    for core in request.cores:
        size = sizes.get(core.project_id)
        if size is None or size >= request.viable:
            continue
        project = await session.get(models.Project, core.project_id)
        if project is None:
            continue
        await raise_alert(
            session,
            AlertKind.FORMATION_UNDERSIZED,
            now,
            title=f"Core below viable size: {project.name}",
            message=f"{size} of {request.viable} members after formation",
            link=f"/admin/events/{event.id}/formation",
            subject_type="project",
            subject_id=project.id,
            subject_title=project.name,
            dedupe_key=f"formation_undersized:{project.id}:{run.round}",
        )
    proposals = list(
        await session.scalars(
            select(models.MergeProposal)
            .where(
                models.MergeProposal.run_id == run.id,
                models.MergeProposal.status == MergeProposalStatus.PENDING.value,
            )
            .order_by(models.MergeProposal.created_at, models.MergeProposal.id)
        )
    )
    for proposal in proposals:
        absorbed = await session.get(models.Project, proposal.absorbed_team_id)
        target = await session.get(models.Project, proposal.target_team_id)
        if absorbed is None or target is None:
            continue
        absorbed_rows = await _live_team_rows(session, absorbed.id)
        target_rows = await _live_team_rows(session, target.id)
        users = await _users_by_id(
            session,
            {row.user_id for row in target_rows}
            | ({target.lead_user_id} if target.lead_user_id is not None else set()),
        )
        target_lead = users.get(target.lead_user_id) if target.lead_user_id is not None else None
        if target_lead is None:
            order = await lead_order(session, target, now)
            target_lead = order[0].user if order else None
        if target_lead is None:
            continue
        merged_size = len(absorbed_rows) + len(target_rows)
        if merged_size < 1:
            continue
        members = tuple(notification_person(users[row.user_id]) for row in target_rows)
        expires_at = target.review_ends_at or formation.review_ends_at or event.starts_at
        reason = next(
            (
                merge.reason
                for merge in plan.merges
                if merge.absorbed_team_id == proposal.absorbed_team_id
                and merge.target_team_id == proposal.target_team_id
            ),
            "Undersized teams fit together",
        )
        recipients = [
            (lead_id, project)
            for lead_id, project in (
                (absorbed.lead_user_id, absorbed),
                (target.lead_user_id, target),
            )
            if lead_id is not None
        ]
        for lead_id, project in recipients:
            await notify(
                session,
                lead_id,
                MergeProposedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    merge_proposal_id=proposal.id,
                    target_project_id=target.id,
                    target_team_name=target.name,
                    target_lead=notification_person(target_lead),
                    members=members,
                    merged_size=merged_size,
                    expires_at=expires_at,
                    reason=reason,
                ),
                now,
                dedupe_key=f"merge_proposed:{proposal.id}:{lead_id}",
            )
    await session.flush()


async def dissolve_core(
    session: AsyncSession,
    event: models.Event,
    project: models.Project,
    now: datetime,
    *,
    reason: str,
) -> None:
    rows = list(
        await session.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
        )
    )
    for row in rows:
        row.left_at = now
        row.left_reason = LeftReason.DISBANDED.value
        enrollment = await live_enrollment_row(session, event.id, row.user_id, for_update=True)
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
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.PROJECT_DISBAND,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"team_status": TeamStatus.FORMING.value, "members": len(rows)},
        after={"team_status": TeamStatus.DISBANDED.value, "reason": reason},
        reason=reason,
    )
    await session.flush()
    return None


async def open_formation_run(
    session: AsyncSession,
    event_id: UUID,
    kind: FormationKind,
    algo_version: str,
    now: datetime,
) -> tuple[models.FormationRun, bool]:
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if event.status != EventStatus.PUBLISHED.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation is available for published hackathons"
        )
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    allowed = (
        {FormationPhase.LOCKED}
        if kind == FormationKind.LATE
        else {FormationPhase.OPEN, FormationPhase.REVIEW}
    )
    if phase not in allowed:
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation cannot run in this phase")
    existing = await session.scalar(
        select(models.FormationRun)
        .where(
            models.FormationRun.event_id == event.id,
            models.FormationRun.kind == kind.value,
            models.FormationRun.status.in_(
                [FormationRunStatus.PENDING.value, FormationRunStatus.RUNNING.value]
            ),
        )
        .order_by(models.FormationRun.created_at.desc(), models.FormationRun.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing, False
    run = models.FormationRun(
        event_id=event.id,
        round=formation.rounds + 1,
        kind=kind.value,
        status=FormationRunStatus.PENDING.value,
        planner=FormationPlanner.ALGO.value,
        input={},
        output=None,
        validation_errors=[],
        algo_version=algo_version,
    )
    session.add(run)
    await session.flush()
    return run, True


async def start_formation_run(
    session: AsyncSession,
    event_id: UUID,
    kind: FormationKind,
    run_id: UUID,
    algo_version: str,
    now: datetime,
) -> tuple[models.FormationRun, FormationRequest] | None:
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    formation = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or formation is None:
        return None
    run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.event_id != event.id:
        return None
    if run.status == FormationRunStatus.APPLIED.value:
        return None
    run.round = formation.rounds + 1
    phase = FormationPhase(formation.phase)
    allowed = (
        {FormationPhase.LOCKED, FormationPhase.FORMING}
        if kind == FormationKind.LATE
        else {FormationPhase.OPEN, FormationPhase.REVIEW, FormationPhase.FORMING}
    )
    if phase not in allowed:
        run.status = FormationRunStatus.FAILED.value
        run.validation_errors = [
            {
                "attempt": 0,
                "planner": FormationPlanner.ALGO.value,
                "plan": None,
                "errors": [],
                "error": f"Formation cannot start in phase {phase}",
            }
        ]
        run.planner = FormationPlanner.ALGO.value
        await session.flush()
        return None
    if phase != FormationPhase.FORMING:
        pre_phase = phase
    elif kind == FormationKind.LATE:
        pre_phase = FormationPhase.LOCKED
    elif formation.rounds > 0:
        pre_phase = FormationPhase.REVIEW
    else:
        pre_phase = FormationPhase.OPEN
    rerun = kind == FormationKind.MAIN and pre_phase == FormationPhase.REVIEW
    scoped = await preserved_projects(session, event.id, kind, pre_phase)
    for project in scoped:
        await close_project_pending_items(session, project.id, now, reason="formation")
        await close_pending_lead_offers(session, project.id, now)
    if rerun:
        for project in scoped:
            proposed = await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                    models.ProjectMember.state == MemberState.PROPOSED.value,
                )
            )
            for row in proposed:
                row.left_at = now
                row.left_reason = LeftReason.REMOVED.value
                enrollment = await live_enrollment_row(
                    session, event.id, row.user_id, for_update=True
                )
                if enrollment is not None and enrollment.status == EnrollmentStatus.PLACED.value:
                    enrollment.status = EnrollmentStatus.ACTIVE.value
                    enrollment.status_at = now
                    enrollment.reserve_rank = None
        await session.flush()
    for project in scoped:
        live_ids = set(
            await session.scalars(
                select(models.ProjectMember.user_id)
                .join(models.User, models.User.id == models.ProjectMember.user_id)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                    models.User.banned_at.is_(None),
                )
            )
        )
        if not live_ids:
            await dissolve_core(
                session,
                event,
                project,
                now,
                reason="No members remained after proposals were released",
            )
        elif (project.lead_user_id is None or project.lead_user_id not in live_ids) and not rerun:
            await dissolve_core(
                session,
                event,
                project,
                now,
                reason="A core without a lead was dissolved at formation",
            )
    request = await build_formation_request(session, event, formation, kind, pre_phase, now)
    formation.phase = FormationPhase.FORMING.value
    run.input = request.model_dump(mode="json")
    run.status = FormationRunStatus.RUNNING.value
    run.validation_errors = []
    run.algo_version = algo_version
    run.round = formation.rounds + 1
    await session.flush()
    return run, request


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    plan: FormationPlan | None
    planner: FormationPlanner
    attempts: list[JsonValue]
    error: str | None


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _attempt(
    attempt: int,
    planner: FormationPlanner,
    plan: FormationPlan | None,
    errors: list[JsonValue],
    error: str | None,
) -> JsonValue:
    return {
        "attempt": attempt,
        "planner": planner.value,
        "plan": plan.model_dump(mode="json") if plan is not None else None,
        "errors": errors,
        "error": error,
    }


def failure_outcome(error: Exception) -> PlanOutcome:
    text = _error_text(error)
    return PlanOutcome(
        plan=None,
        planner=FormationPlanner.ALGO,
        attempts=[_attempt(1, FormationPlanner.ALGO, None, [], text)],
        error=text,
    )


def _fallback_outcome(
    request: FormationRequest, settings: AlgoSettings, attempt: int, attempts: list[JsonValue]
) -> PlanOutcome:
    plan = fallback_plan(request, settings)
    errors = validate_plan(request, plan, settings)
    attempts.append(_attempt(attempt, FormationPlanner.FALLBACK, plan, errors, None))
    return PlanOutcome(
        plan=plan if not errors else None,
        planner=FormationPlanner.FALLBACK,
        attempts=attempts,
        error=None if not errors else "Fallback plan is invalid",
    )


async def plan_formation(runtime: Runtime, request: FormationRequest) -> PlanOutcome:
    async with runtime.sessionmaker.begin() as session:
        ctx = await runtime.algo_context(session)
    settings = ctx.settings
    attempts: list[JsonValue] = []
    if len(request.unplaced) > FORMATION_ALGO_LIMIT:
        return _fallback_outcome(request, settings, 1, attempts)
    for attempt in (1, 2):
        plan: FormationPlan | None
        errors: list[JsonValue]
        error: str | None = None
        try:
            plan = await call_algo("form_teams", runtime.algorithm.form_teams(ctx, request))
            errors = validate_plan(request, plan, settings)
        except Exception as failure:
            plan, errors, error = None, [], _error_text(failure)
        attempts.append(_attempt(attempt, FormationPlanner.ALGO, plan, errors, error))
        if plan is not None and error is None and not errors:
            return PlanOutcome(
                plan=plan, planner=FormationPlanner.ALGO, attempts=attempts, error=None
            )
    return _fallback_outcome(request, settings, 3, attempts)


async def fail_formation_run(
    session: AsyncSession, run_id: UUID, outcome: PlanOutcome, now: datetime
) -> None:
    run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.status in {
        FormationRunStatus.APPLIED.value,
        FormationRunStatus.FAILED.value,
    }:
        return None
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    formation = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or formation is None:
        return None
    run.status = FormationRunStatus.FAILED.value
    run.output = None
    run.validation_errors = outcome.attempts
    run.planner = outcome.planner.value
    run.applied_at = None
    if FormationKind(run.kind) == FormationKind.LATE:
        formation.phase = FormationPhase.LOCKED.value
    elif formation.rounds > 0:
        formation.phase = FormationPhase.REVIEW.value
    else:
        settings = await load_settings(session)
        formation.phase = FormationPhase.OPEN.value
        formation.formation_at = now + timedelta(hours=1)
        formation.review_ends_at = min(
            formation.formation_at + timedelta(hours=settings.review_hours),
            formation_timing(event, settings, now).team_deadline,
        )
    await session.flush()
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.FORMATION,
        entity_id=event.id,
        before={"run_id": str(run.id), "phase": FormationPhase.FORMING.value},
        after={
            "phase": formation.phase,
            "planner": outcome.planner.value,
            "error": outcome.error,
        },
        reason=None,
    )
    await raise_alert(
        session,
        AlertKind.FORMATION_FAILED,
        now,
        title=f"Formation failed: {event.title}",
        message=outcome.error or "Formation could not build a valid plan",
        link=f"/admin/events/{event.id}/formation",
        subject_type="event",
        subject_id=event.id,
        subject_title=event.title,
        dedupe_key=f"formation_failed:{event.id}:{run.id}",
    )
    await session.flush()
    return None


RESERVE_RANK_REASON = "Ranked by the reserve planner"


def validated_reserve_ranking(
    reserve_ids: Sequence[UUID], ranked: Sequence[RankedMember]
) -> list[RankedMember]:
    allowed = set(reserve_ids)
    kept: list[RankedMember] = []
    users: set[UUID] = set()
    ranks: set[int] = set()
    for entry in ranked:
        if entry.user_id not in allowed or entry.user_id in users or entry.rank in ranks:
            continue
        kept.append(entry)
        users.add(entry.user_id)
        ranks.add(entry.rank)
    result = [
        RankedMember(user_id=entry.user_id, rank=index, reason=entry.reason or RESERVE_RANK_REASON)
        for index, entry in enumerate(sorted(kept, key=lambda entry: entry.rank), start=1)
    ]
    for user_id in reserve_ids:
        if user_id in users:
            continue
        users.add(user_id)
        result.append(
            RankedMember(user_id=user_id, rank=len(result) + 1, reason=RESERVE_RANK_REASON)
        )
    return result


async def build_reserve_request(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    now: datetime,
) -> ReserveRequest | None:
    from hmm_core.catalog import service as catalog

    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    reserve_rows = list(
        await session.scalars(
            select(models.Enrollment)
            .join(models.User, models.User.id == models.Enrollment.user_id)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                models.User.banned_at.is_(None),
            )
            .order_by(models.Enrollment.created_at, models.Enrollment.id)
        )
    )
    if not reserve_rows:
        return None
    projects_rows = list(
        await session.scalars(
            select(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status.in_(
                    sorted(status.value for status in CAPACITY_TEAM_STATES)
                ),
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    )
    project_ids = [project.id for project in projects_rows]
    member_rows: list[models.ProjectMember] = (
        list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id.in_(project_ids),
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )
        if project_ids
        else []
    )
    member_ids = {row.user_id for row in member_rows}
    enrollments: dict[UUID, models.Enrollment] = (
        {
            enrollment.user_id: enrollment
            for enrollment in await session.scalars(
                select(models.Enrollment).where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.user_id.in_(member_ids),
                    models.Enrollment.status.in_(
                        sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                    ),
                )
            )
        }
        if member_ids
        else {}
    )
    area_ids = {row.area_id for row in member_rows if row.area_id is not None} | {
        row.preferred_area_id for row in reserve_rows if row.preferred_area_id is not None
    }
    areas: dict[UUID, str] = (
        {
            area_id: key
            for area_id, key in await session.execute(
                select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )
    snapshots = await build_member_snapshots(
        session, sorted(member_ids | {row.user_id for row in reserve_rows}), now
    )
    teams: list[TeamComposition] = []
    for project in projects_rows:
        members: list[TeamMemberSnapshot] = []
        for row in member_rows:
            if row.project_id != project.id or row.user_id not in snapshots:
                continue
            enrollment = enrollments.get(row.user_id)
            members.append(
                TeamMemberSnapshot(
                    **snapshots[row.user_id].model_dump(),
                    role=MemberRole(row.role),
                    area=areas.get(row.area_id) if row.area_id is not None else None,
                    state=MemberState(row.state),
                    willing_to_lead=enrollment.willing_to_lead if enrollment is not None else False,
                    claimed_lead=row.user_id == project.lead_user_id,
                    enrolled_at=enrollment.created_at if enrollment is not None else row.joined_at,
                )
            )
        teams.append(
            TeamComposition(
                project_id=project.id,
                members=members,
                open_seats=max(0, sizes.target - len(members)),
            )
        )
    reserve = [
        EnrolledMember(
            **snapshots[row.user_id].model_dump(),
            preferred_area=areas.get(row.preferred_area_id)
            if row.preferred_area_id is not None
            else None,
            willing_to_lead=row.willing_to_lead,
            enrolled_at=row.created_at,
            note=row.note or "",
        )
        for row in reserve_rows
        if row.user_id in snapshots
    ]
    return ReserveRequest(
        hackathon=await catalog.event_snapshot(session, event.id), teams=teams, reserve=reserve
    )


async def apply_reserve_ranking(
    session: AsyncSession,
    event_id: UUID,
    ranked: Sequence[RankedMember] | None,
) -> None:
    rows = list(
        await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not rows:
        return None
    if ranked is None:
        next_rank = max(
            (row.reserve_rank for row in rows if row.reserve_rank is not None), default=0
        )
        for row in sorted(
            (row for row in rows if row.reserve_rank is None),
            key=lambda row: (row.created_at, str(row.id)),
        ):
            next_rank += 1
            row.reserve_rank = next_rank
    else:
        final = validated_reserve_ranking([row.user_id for row in rows], ranked)
        ranks = {entry.user_id: entry.rank for entry in final}
        for row in rows:
            rank = ranks.get(row.user_id)
            if rank is not None:
                row.reserve_rank = rank
    await session.flush()
    return None


async def reserve_ranking_events(session: AsyncSession) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.Event.id)
            .join(models.EventFormation, models.EventFormation.event_id == models.Event.id)
            .where(
                models.Event.formation_enabled.is_(True),
                models.Event.status == EventStatus.PUBLISHED.value,
                models.EventFormation.phase.in_(
                    [FormationPhase.REVIEW.value, FormationPhase.LOCKED.value]
                ),
                exists().where(
                    models.Enrollment.event_id == models.Event.id,
                    models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                ),
            )
            .order_by(models.Event.id)
        )
    )


async def update_formation(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateFormationBody,
    actor: models.User,
    now: datetime,
) -> schemas.Formation:
    event = await locked_formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if phase != FormationPhase.OPEN:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation settings are frozen once formation starts"
        )
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    changes = body.model_dump(exclude_unset=True)
    target = changes.get("target_override", formation.target_override)
    if target is not None and not sizes.min_size <= target <= sizes.max_size:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR,
            f"target_override must be between {sizes.min_size} and {sizes.max_size}",
        )
    override = changes.get("formation_at_override", formation.formation_at_override)
    if override is not None and override <= now:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR, "formation_at_override must be in the future"
        )
    before: dict[str, JsonValue] = {
        "target_override": formation.target_override,
        "formation_at_override": formation.formation_at_override.isoformat()
        if formation.formation_at_override is not None
        else None,
        "formation_at": formation.formation_at.isoformat(),
        "review_ends_at": formation.review_ends_at.isoformat()
        if formation.review_ends_at is not None
        else None,
    }
    previous = formation.formation_at
    if "target_override" in changes:
        formation.target_override = changes["target_override"]
    if "formation_at_override" in changes:
        formation.formation_at_override = changes["formation_at_override"]
        timing = formation_timing(event, settings, now)
        candidate = formation.formation_at_override or timing.formation_at
        if candidate != formation.formation_at:
            formation.formation_at = candidate
            formation.review_ends_at = min(
                candidate + timedelta(hours=settings.review_hours), timing.team_deadline
            )
    after: dict[str, JsonValue] = {
        "target_override": formation.target_override,
        "formation_at_override": formation.formation_at_override.isoformat()
        if formation.formation_at_override is not None
        else None,
        "formation_at": formation.formation_at.isoformat(),
        "review_ends_at": formation.review_ends_at.isoformat()
        if formation.review_ends_at is not None
        else None,
    }
    if after != before:
        await session.flush()
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.ADMIN,
            action=AuditAction.FORMATION_EDIT,
            entity_type=AuditEntityType.FORMATION,
            entity_id=event.id,
            before=before,
            after=after,
            reason=body.reason,
        )
        if formation.formation_at != previous and abs(
            formation.formation_at - previous
        ) > timedelta(hours=6):
            enrolled = await session.scalars(
                select(models.Enrollment.user_id)
                .where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.status.in_(
                        sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                    ),
                )
                .order_by(models.Enrollment.created_at, models.Enrollment.id)
            )
            leads = await session.scalars(
                select(models.Project.lead_user_id)
                .where(
                    models.Project.event_id == event.id,
                    models.Project.lead_user_id.is_not(None),
                )
                .order_by(models.Project.created_at, models.Project.id)
            )
            ids = [*enrolled, *(lead for lead in leads if lead is not None)]
            await notify_many(
                session,
                ids,
                FormationRescheduledPayload(
                    link=f"/events/{event.id}/formation",
                    event_id=event.id,
                    event_title=event.title,
                    previous_formation_at=previous,
                    formation_at=formation.formation_at,
                    timezone=event.timezone,
                    reason=body.reason or "Admin changed the formation schedule",
                ),
                now,
                dedupe_key=f"formation_rescheduled:{event.id}:{formation.formation_at.isoformat()}",
            )
        await session.flush()
    view = await formation_participation(session, event, actor, now)
    if view is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return view


async def request_formation_run(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> models.FormationRun:
    run, created = await open_formation_run(session, event_id, FormationKind.MAIN, "", now)
    if not created:
        return run
    formation = await session.get(models.EventFormation, event_id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.FORMATION_RUN,
        entity_type=AuditEntityType.FORMATION,
        entity_id=event_id,
        before={"phase": formation.phase, "rounds": formation.rounds},
        after={"run_id": str(run.id), "kind": FormationKind.MAIN.value, "round": run.round},
        reason=body.reason if body is not None else None,
    )
    await defer_job(
        session,
        jobs,
        JobName.RUN_FORMATION,
        event_id=str(event_id),
        run_id=str(run.id),
        kind=FormationKind.MAIN.value,
    )
    await session.refresh(run)
    return run


def formation_run_view(row: models.FormationRun) -> schemas.FormationRun:
    return schemas.FormationRun(
        id=row.id,
        event_id=row.event_id,
        round=row.round,
        kind=FormationKind(row.kind),
        status=FormationRunStatus(row.status),
        planner=FormationPlanner(row.planner) if row.planner else None,
        input=FormationRequest.model_validate(row.input)
        if isinstance(row.input, dict) and row.input
        else None,
        output=FormationPlan.model_validate(row.output)
        if isinstance(row.output, dict) and row.output
        else None,
        validation_errors=row.validation_errors if isinstance(row.validation_errors, list) else [],
        algo_version=row.algo_version or None,
        created_at=row.created_at,
        applied_at=row.applied_at,
    )


async def list_formation_runs(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> list[schemas.FormationRun]:
    event = await formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    rows = await session.scalars(
        select(models.FormationRun)
        .where(models.FormationRun.event_id == event.id)
        .order_by(models.FormationRun.created_at.desc(), models.FormationRun.id.desc())
        .limit(50)
    )
    return [formation_run_view(row) for row in rows]
