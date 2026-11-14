from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectStatus,
)
from hmm_contracts.notifications import (
    NotificationPerson,
    ProjectMemberLeftPayload,
    ProjectMemberRemovedPayload,
)
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas
from hmm_core.projects.common import project_member_ids
from hmm_core.projects.lifecycle import _locked_project
from hmm_core.projects.records import led_projects_count
from hmm_core.projects.views import (
    CLOSED_PROJECT_STATUSES,
    POST_RESULT_TEAM_STATUSES,
    project_response,
)


async def remove_project_member(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    user_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> None:
    from hmm_core.formation import service as formation
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None:
        await formation.review_remove_member(session, jobs, project_id, user_id, actor, now)
        return
    project = await _locked_project(session, project_id)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if user_id == actor.id:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "You cannot remove yourself; leave the project instead"
        )
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    previous: dict[str, JsonValue] = {"role": member.role, "state": member.state}
    member.left_at = now
    member.left_reason = LeftReason.REMOVED.value
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    removed = await session.get(models.User, user_id)
    if removed is not None:
        await notify(
            session,
            removed.id,
            ProjectMemberRemovedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=NotificationPerson(user_id=removed.id, display_name=removed.display_name),
                actor=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
                reason=body.reason if body is not None else None,
            ),
            now,
            dedupe_key=f"project_member_removed:{member.id}",
        )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.MEMBER_REMOVE,
        entity_type=AuditEntityType.PROJECT_MEMBER,
        entity_id=member.id,
        before=previous,
        after={"left_reason": LeftReason.REMOVED.value},
        reason=body.reason if body is not None else None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def leave_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    from hmm_core.formation import service as formation
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None:
        await formation.leave_team(session, jobs, project_id, actor, now)
        return
    project = await _locked_project(session, project_id)
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    was_lead = project.lead_user_id == actor.id
    member.left_at = now
    member.left_reason = LeftReason.LEFT.value
    if was_lead:
        project.lead_user_id = None
        project.last_lead_action_at = now
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
    await session.flush()
    await session.refresh(project)
    remaining = await project_member_ids(session, project.id)
    await notify_many(
        session,
        remaining,
        ProjectMemberLeftPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            member=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
            member_count=len(remaining),
        ),
        now,
        dedupe_key=f"project_member_left:{member.id}",
    )
    if was_lead:
        await formation.start_succession(
            session,
            project,
            None,
            now,
            exclude={actor.id},
            reason="The lead left the project",
        )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def claim_project_lead(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.formation import service as formation
    from hmm_core.identity.service import lock_user
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Claim the lead through the hackathon flow")
    await lock_user(session, actor.id)
    project = await _locked_project(session, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Claim the lead through the hackathon flow")
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.lead_user_id is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project already has a lead")
    if project.status != ProjectStatus.PAUSED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is not paused")
    if (
        await led_projects_count(session, actor.id)
        >= (await load_settings(session)).projects_led_max
    ):
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    project.lead_user_id = actor.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    member.role = MemberRole.LEAD.value
    member.state = MemberState.CONFIRMED.value
    member.confirmed_at = member.confirmed_at or now
    member.confirm_by = None
    member.confirm_kind = None
    await formation.close_pending_lead_offers(session, project.id, now)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": None},
        after={"lead_user_id": str(actor.id), "source": "claim"},
        reason=None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    await session.refresh(project)
    return await project_response(session, project.id, actor, now)
