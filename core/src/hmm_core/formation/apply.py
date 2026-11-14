from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.algo import FormationPlan, FormationRequest
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ConfirmKind,
    EnrollmentStatus,
    ErrorCode,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
    JoinedVia,
    LeadOfferSource,
    LeftReason,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import (
    AlertKind,
    FormationReadyPayload,
    MergeProposedPayload,
    ReservePlacedPayload,
    TeamProposedPayload,
    TeamReconfirmPayload,
)
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    LIVE_ENROLLMENT_STATES,
    enqueue_participation_jobs,
    formation_timing,
    live_enrollment_row,
    notification_person,
)
from hmm_core.formation.lead_offers import create_lead_offer, lead_order
from hmm_core.formation.leads import close_project_pending_items
from hmm_core.formation.planning import validate_plan
from hmm_core.notifications.service import notify, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings


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
