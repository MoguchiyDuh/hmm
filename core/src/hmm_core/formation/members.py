from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    ErrorCode,
    FormationPhase,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectStatus,
    TeamStatus,
)
from hmm_contracts.notifications import AlertKind, LeadSteppedDownPayload, TeamMemberLeftPayload
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import service as attendance
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    CLOSED_TEAM_STATUSES,
    RESULT_TEAM_STATUSES,
    TERMINAL_PROJECT_STATUSES,
    enqueue_participation_jobs,
    formation_row,
    live_enrollment_row,
    locked_formation_event,
    notification_person,
)
from hmm_core.formation.enrollments import resolve_preferred_area
from hmm_core.formation.lead_offers import (
    close_pending_lead_offers,
    create_lead_offer,
    lead_order,
    start_succession,
    team_member_ids,
)
from hmm_core.formation.leads import close_project_pending_items, stored_lead_fit
from hmm_core.notifications.service import notify_many, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects


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
    await note_lead_action(session, project, actor, now)
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


async def team_member_view(
    session: AsyncSession,
    project: models.Project,
    member: models.ProjectMember,
    actor: models.User,
) -> project_schemas.ProjectMember:
    user = await session.get_one(models.User, member.user_id)
    area_key = (
        None
        if member.area_id is None
        else await session.scalar(select(models.Area.key).where(models.Area.id == member.area_id))
    )
    return projects.project_member(
        project, projects.MemberRow(member=member, user=user, area_key=area_key), actor
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


async def _placement_target(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> tuple[models.Project, models.Event, models.ProjectMember]:
    project = await session.get(models.Project, project_id)
    if project is None or project.event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, project.event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status not in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value):
        raise ProblemError(ErrorCode.INVALID_STATE, "Placements cannot be confirmed now")
    member = await _own_team_row(session, project.id, actor.id)
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return project, event, member


async def confirm_placement(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> tuple[models.Project, models.ProjectMember]:
    project, _, member = await _placement_target(session, project_id, actor)
    if member.state == MemberState.CONFIRMED.value:
        return project, member
    member.state = MemberState.CONFIRMED.value
    member.confirmed_at = now
    member.confirm_by = None
    member.confirm_kind = None
    await session.flush()
    return project, member


async def decline_placement(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.DeclinePlacementBody,
    actor: models.User,
    now: datetime,
) -> models.Enrollment:
    project, event, member = await _placement_target(session, project_id, actor)
    if member.state != MemberState.PROPOSED.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "You have no pending placement; leave the team instead"
        )
    was_lead = project.lead_user_id == actor.id
    if was_lead:
        member.role = MemberRole.MEMBER.value
    member.left_at = now
    member.left_reason = LeftReason.DECLINED.value
    row = await live_enrollment_row(session, event.id, actor.id, for_update=True)
    if row is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "You have no enrollment in this event")
    row.status = (
        EnrollmentStatus.WITHDRAWN.value if body.withdraw else EnrollmentStatus.RESERVE.value
    )
    row.status_at = now
    row.reserve_rank = None
    if body.withdraw:
        await attendance.sync_going(session, event.id, actor.id, going=False, now=now)
    await session.flush()
    remaining = await team_member_ids(session, project.id)
    await open_seat(session, project, event, now)
    if remaining:
        await notify_many(
            session,
            remaining,
            TeamMemberLeftPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                member=notification_person(actor),
                member_count=len(remaining),
                reason="The member declined the placement",
            ),
            now,
            dedupe_key=f"team_member_left:{project.id}:{actor.id}",
        )
    if was_lead:
        project.lead_user_id = None
        project.last_lead_action_at = now
        await close_pending_lead_offers(session, project.id, now)
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(actor.id)},
            after={"lead_user_id": None},
            reason="The claimed lead declined the placement",
        )
        await start_succession(
            session,
            project,
            event,
            now,
            exclude={actor.id},
            reason="The claimed lead declined the placement",
        )
    await enqueue_participation_jobs(session, jobs, event.id, reserve=not body.withdraw)
    await session.flush()
    await session.refresh(row)
    return row


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


async def review_remove_member(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    user_id: UUID,
    actor: models.User,
    now: datetime,
) -> EnrollmentStatus | None:
    project = await session.get(models.Project, project_id)
    if project is None or project.event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, project.event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status != TeamStatus.REVIEW.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Members can only be removed while the team is in review"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if user_id == actor.id:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "You cannot remove yourself; leave the team instead"
        )
    member = await _own_team_row(session, project.id, user_id)
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await note_lead_action(session, project, actor, now)
    previous: dict[str, JsonValue] = {"state": member.state, "role": member.role}
    status = await remove_member_from_team(
        session,
        project,
        event,
        member,
        now,
        left_reason=LeftReason.REMOVED,
        notice_reason="The lead removed the member from the team",
        audit_actor=actor.id,
        audit_kind=ActorKind.USER,
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.MEMBER_REMOVE,
        entity_type=AuditEntityType.PROJECT_MEMBER,
        entity_id=member.id,
        before=previous,
        after={
            "left_reason": LeftReason.REMOVED.value,
            "enrollment_status": status.value if status is not None else None,
        },
        reason=None,
    )
    await enqueue_participation_jobs(
        session, jobs, event.id, reserve=status == EnrollmentStatus.RESERVE
    )
    await session.flush()
    return status


async def update_member_area(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    body: schemas.UpdateMemberBody,
    actor: models.User,
    now: datetime,
) -> tuple[models.Project, models.ProjectMember]:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.event_id is not None:
        event = await locked_formation_event(session, project.event_id, actor)
        if not event.formation_enabled:
            raise ProblemError(ErrorCode.NOT_FOUND)
        formation = await formation_row(session, event.id)
        if FormationPhase(formation.phase) == FormationPhase.FORMING:
            raise ProblemError(
                ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly"
            )
    elif project.status in TERMINAL_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None and project.team_status in CLOSED_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    member = await session.scalar(
        select(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    area_id = await resolve_preferred_area(session, body.area_key)
    if member.area_id == area_id:
        return project, member
    member.area_id = area_id
    await note_lead_action(session, project, actor, now)
    await session.flush()
    await session.refresh(project)
    await session.refresh(member)
    return project, member
