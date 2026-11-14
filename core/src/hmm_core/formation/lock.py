from datetime import datetime
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    EventStatus,
    FormationPhase,
    LeadOfferStatus,
    LeftReason,
    MemberState,
    MergeProposalStatus,
    ProjectStatus,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_contracts.notifications import (
    AlertKind,
    PlacementLapsedPayload,
    TeamDisbandedPayload,
    TeamLockedPayload,
)
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    FormationTiming,
    enqueue_participation_jobs,
    formation_row,
    formation_timing,
    live_enrollment_row,
    notification_person,
)
from hmm_core.formation.lead_offers import (
    close_pending_lead_offers,
    force_lead,
    start_succession,
    team_member_ids,
)
from hmm_core.formation.leads import close_project_pending_items, formation_sizes
from hmm_core.formation.members import open_seat
from hmm_core.notifications.service import notify, notify_many, raise_alert, resolve_alerts
from hmm_core.platform.audit import record_audit
from hmm_core.platform.settings import load_settings


async def _close_team_swap_merge_proposals(session: AsyncSession, project_id: UUID) -> None:
    await session.execute(
        update(models.SwapRequest)
        .where(
            models.SwapRequest.status == SwapRequestStatus.PENDING.value,
            or_(
                models.SwapRequest.from_team_id == project_id,
                models.SwapRequest.to_team_id == project_id,
            ),
        )
        .values(status=SwapRequestStatus.CLOSED.value)
    )
    await session.execute(
        update(models.MergeProposal)
        .where(
            models.MergeProposal.status == MergeProposalStatus.PENDING.value,
            or_(
                models.MergeProposal.absorbed_team_id == project_id,
                models.MergeProposal.target_team_id == project_id,
            ),
        )
        .values(status=MergeProposalStatus.CLOSED.value)
    )


async def lock_team(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    project: models.Project,
    now: datetime,
    *,
    main_round: bool,
) -> bool:
    formation = await formation_row(session, event.id)
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    timing = formation_timing(event, settings, now)
    if project.team_status != TeamStatus.REVIEW.value:
        return False
    locked_project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_project is None:
        return False
    project = locked_project
    previous_lead = project.lead_user_id
    member_query = (
        select(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.is_team_row.is_(True),
        )
        .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        .execution_options(populate_existing=True)
    )
    members = list(await session.scalars(member_query))
    for member in members:
        if (
            member.state == MemberState.PROPOSED.value
            and member.confirm_by is not None
            and member.confirm_by <= now
        ):
            await _confirm_deadline_lapse(
                session,
                project,
                event,
                member,
                now,
                timing=timing,
                reason="The confirmation deadline passed",
                succession=False,
            )
    live = list(await session.scalars(member_query))
    if not any(member.state == MemberState.CONFIRMED.value for member in live):
        for member in live:
            member.left_at = now
            member.left_reason = LeftReason.DISBANDED.value
            row = await live_enrollment_row(session, event.id, member.user_id, for_update=True)
            if row is not None:
                row.status = EnrollmentStatus.RESERVE.value
                row.status_at = now
                row.reserve_rank = None
            await notify(
                session,
                member.user_id,
                TeamDisbandedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    reason="No member confirmed the placement",
                ),
                now,
                dedupe_key=f"team_disbanded:{project.id}:{member.user_id}",
            )
        project.team_status = TeamStatus.DISBANDED.value
        project.status = ProjectStatus.DISBANDED.value
        project.lead_user_id = None
        project.ready_at = None
        project.last_lead_action_at = now
        await close_pending_lead_offers(session, project.id, now)
        await close_project_pending_items(session, project.id, now, reason="team_disbanded")
        await _close_team_swap_merge_proposals(session, project.id)
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.PROJECT_DISBAND,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={
                "team_status": TeamStatus.REVIEW.value,
                "lead_user_id": str(previous_lead) if previous_lead is not None else None,
            },
            after={"team_status": TeamStatus.DISBANDED.value, "reason": "no_confirmed_members"},
            reason="No member confirmed the placement",
        )
        await session.flush()
        return False
    await close_pending_lead_offers(session, project.id, now)
    await _close_team_swap_merge_proposals(session, project.id)
    await close_project_pending_items(session, project.id, now, reason="team_locked")
    project.team_status = TeamStatus.LOCKED.value
    project.status = ProjectStatus.ACTIVE.value
    project.locked_at = now
    await session.flush()
    if project.lead_user_id is None:
        await force_lead(
            session, project, event, now, reason="The review window ended without a lead"
        )
    ids = await team_member_ids(session, project.id)
    if len(ids) < sizes.target:
        await open_seat(session, project, event, now)
    reserve_count = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
            )
        )
        or 0
    )
    if len(ids) >= sizes.viable:
        await resolve_alerts(
            session,
            f"formation_undersized:{project.id}:{formation.rounds}",
            now,
            note="The team reached the viable size",
        )
    if len(ids) < sizes.min_size and reserve_count == 0:
        await raise_alert(
            session,
            AlertKind.TEAM_BELOW_MIN,
            now,
            title=f"Team below minimum size: {project.name}",
            message=f"{len(ids)} of {sizes.min_size} members with an empty reserve",
            link=f"/admin/projects/{project.id}",
            subject_type="project",
            subject_id=project.id,
            subject_title=project.name,
            dedupe_key=f"team_below_min:{project.id}",
        )
    if main_round:
        people = list(
            await session.scalars(
                select(models.User).where(models.User.id.in_(ids)).order_by(models.User.id)
            )
        )
        lead = await session.get(models.User, project.lead_user_id)
        if lead is not None:
            await notify_many(
                session,
                ids,
                TeamLockedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    members=tuple(notification_person(user) for user in people),
                    lead=notification_person(lead),
                    starts_at=event.starts_at,
                    timezone=event.timezone,
                    reserve_count=reserve_count,
                ),
                now,
                dedupe_key=f"team_locked:{project.id}",
            )
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={
            "team_status": TeamStatus.REVIEW.value,
            "lead_user_id": str(previous_lead) if previous_lead is not None else None,
        },
        after={
            "team_status": TeamStatus.LOCKED.value,
            "lead_user_id": str(project.lead_user_id) if project.lead_user_id is not None else None,
            "locked_at": now.isoformat(),
        },
        reason=None,
    )
    await session.flush()
    return True


async def lock_due_teams(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    now: datetime,
    *,
    limit: int = 200,
) -> int:
    if event.status == EventStatus.CANCELLED.value:
        return 0
    formation = await formation_row(session, event.id)
    main_round = FormationPhase(formation.phase) == FormationPhase.REVIEW
    if FormationPhase(formation.phase) not in (FormationPhase.REVIEW, FormationPhase.LOCKED):
        return 0
    due_query = select(models.Project.id).where(
        models.Project.event_id == event.id,
        models.Project.team_status == TeamStatus.REVIEW.value,
        func.coalesce(models.Project.review_ends_at, formation.review_ends_at) <= now,
    )
    project_ids = list(
        await session.scalars(
            due_query.order_by(models.Project.created_at, models.Project.id).limit(limit)
        )
    )
    locked = 0
    disbanded = False
    for project_id in project_ids:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            continue
        if await lock_team(session, jobs, event, project, now, main_round=main_round):
            locked += 1
        else:
            disbanded = True
    if locked or disbanded:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=disbanded)
    if (
        main_round
        and formation.review_ends_at is not None
        and formation.review_ends_at <= now
        and await session.scalar(due_query.limit(1)) is None
    ):
        formation.phase = FormationPhase.LOCKED.value
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.FORMATION,
            entity_id=event.id,
            before={"phase": FormationPhase.REVIEW.value},
            after={"phase": FormationPhase.LOCKED.value},
            reason=None,
        )
    await session.flush()
    return locked


async def lapse_due_proposals(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    now: datetime,
    *,
    limit: int = 200,
) -> int:
    await formation_row(session, event.id)
    timing = formation_timing(event, await load_settings(session), now)
    rows = (
        await session.execute(
            select(models.ProjectMember, models.Project)
            .join(models.Project, models.Project.id == models.ProjectMember.project_id)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.LOCKED.value,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.ProjectMember.state == MemberState.PROPOSED.value,
                models.ProjectMember.confirm_by.is_not(None),
                models.ProjectMember.confirm_by <= now,
            )
            .order_by(models.ProjectMember.confirm_by, models.ProjectMember.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    ).all()
    projects_by_id: dict[UUID, models.Project] = {}
    count = 0
    for member, candidate_project in rows:
        project = projects_by_id.get(candidate_project.id)
        if project is None:
            project = (
                await session.scalars(
                    select(models.Project)
                    .where(models.Project.id == candidate_project.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).one()
            projects_by_id[project.id] = project
        await _confirm_deadline_lapse(
            session,
            project,
            event,
            member,
            now,
            timing=timing,
            reason="The confirmation deadline passed",
            succession=True,
        )
        count += 1
    if count:
        await enqueue_participation_jobs(session, jobs, event.id, reserve=False)
    await session.flush()
    return count


async def _confirm_deadline_lapse(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    member: models.ProjectMember,
    now: datetime,
    *,
    timing: FormationTiming,
    reason: str,
    succession: bool,
) -> None:
    was_lead = project.lead_user_id == member.user_id
    confirm_by = cast(datetime, member.confirm_by)
    confirm_kind = member.confirm_kind
    member.left_at = now
    member.left_reason = LeftReason.LAPSED.value
    row = await live_enrollment_row(session, event.id, member.user_id, for_update=True)
    if row is not None:
        row.status = EnrollmentStatus.LAPSED.value
        row.status_at = now
        row.reserve_rank = None
    await session.flush()
    await open_seat(session, project, event, now)
    await session.execute(
        update(models.LeadOffer)
        .where(
            models.LeadOffer.project_id == project.id,
            models.LeadOffer.user_id == member.user_id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
        )
        .values(status=LeadOfferStatus.CLOSED.value, responded_at=now)
    )
    await notify(
        session,
        member.user_id,
        PlacementLapsedPayload(
            link=f"/events/{event.id}/formation",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            confirm_by=confirm_by,
            reason=reason,
            reg_deadline=timing.reg_deadline,
        ),
        now,
        dedupe_key=f"placement_lapsed:{project.id}:{member.user_id}",
    )
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.PROJECT_MEMBER,
        entity_id=member.id,
        before={"state": MemberState.PROPOSED.value, "confirm_kind": confirm_kind},
        after={
            "left_reason": LeftReason.LAPSED.value,
            "enrollment_status": EnrollmentStatus.LAPSED.value,
        },
        reason=reason,
    )
    if was_lead:
        project.lead_user_id = None
        project.last_lead_action_at = now
        await close_pending_lead_offers(session, project.id, now)
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(member.user_id)},
            after={"lead_user_id": None},
            reason=reason,
        )
        if succession:
            await start_succession(
                session, project, event, now, exclude={member.user_id}, reason=reason
            )
    await session.flush()
