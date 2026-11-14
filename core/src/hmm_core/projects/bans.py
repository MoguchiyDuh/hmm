from datetime import datetime
from uuid import UUID

from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, LeftReason
from hmm_contracts.notifications import NotificationPerson, ProjectMemberLeftPayload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.projects.common import project_member_ids


async def remove_user_from_standalone_projects(
    session: AsyncSession,
    user_id: UUID,
    actor: UUID,
    actor_kind: ActorKind,
    now: datetime,
) -> None:
    from hmm_core.formation import service as formation

    user = await session.get(models.User, user_id)
    if user is None:
        return None
    project_ids = sorted(
        set(
            await session.scalars(
                select(models.Project.id)
                .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
                .where(
                    models.ProjectMember.user_id == user_id,
                    models.ProjectMember.left_at.is_(None),
                    models.Project.team_status.is_(None),
                )
            )
        ),
        key=str,
    )
    for project_id in project_ids:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            continue
        member = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is None:
            continue
        was_lead = project.lead_user_id == user_id
        member.left_at = now
        member.left_reason = LeftReason.BANNED.value
        if was_lead:
            project.lead_user_id = None
            project.last_lead_action_at = now
        await formation.close_pending_lead_offers(session, project.id, now)
        if was_lead:
            await record_audit(
                session,
                actor=actor,
                actor_kind=actor_kind,
                action=AuditAction.LEAD_STEP_DOWN,
                entity_type=AuditEntityType.PROJECT,
                entity_id=project.id,
                before={"lead_user_id": str(user_id)},
                after={"lead_user_id": None},
                reason="banned",
            )
        if project.lead_user_id is None:
            await formation.start_succession(
                session,
                project,
                None,
                now,
                exclude={user_id},
                reason="The banned lead left the project"
                if was_lead
                else "The offered member was banned",
            )
        await session.flush()
        await session.refresh(project)
        remaining = await project_member_ids(session, project.id)
        if remaining:
            await notify_many(
                session,
                remaining,
                ProjectMemberLeftPayload(
                    link=f"/projects/{project.id}",
                    project_id=project.id,
                    project_name=project.name,
                    member=NotificationPerson(user_id=user.id, display_name=user.display_name),
                    member_count=len(remaining),
                ),
                now,
                dedupe_key=f"project_member_left:{member.id}",
            )
