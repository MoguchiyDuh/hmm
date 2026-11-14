from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import ceil
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import AlgoSettings
from hmm_contracts.algo import LeadFit as AlgoLeadFit
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventStatus,
    FormationPhase,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import FormationRescheduledPayload, TeamDisbandedPayload
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    LEAD_FIT_PHASES,
    LIVE_ENROLLMENT_STATES,
    ONBOARDED_STATUSES,
    TERMINAL_PROJECT_STATUSES,
    dnd_active,
    enqueue_participation_jobs,
    formation_event,
    formation_row,
    formation_timing,
    latest_terminal_enrollment,
    lead_eligibility,
    live_enrollment_row,
    locked_formation_event,
)
from hmm_core.formation.enrollments import live_membership
from hmm_core.notifications.service import notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.schemas import UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects


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
