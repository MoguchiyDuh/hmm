from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ActorKind, AuditAction, AuditEntityType, LeftReason
from hmm.notifications.payloads import NotificationPerson, ProjectMemberLeftPayload
from hmm.notifications.service import notify_many
from hmm.platform.audit import record_audit
from hmm.platform.db import locked
from hmm.projects.common import project_member_ids


async def remove_user_from_standalone_projects(
    session: AsyncSession,
    user_id: UUID,
    actor: UUID,
    actor_kind: ActorKind,
    now: datetime,
) -> None:
    from hmm.projects.succession import standalone_lead_succession

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
        project = await locked(session, models.Project, project_id)
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
            await standalone_lead_succession(
                session,
                project,
                now,
                exclude={user_id},
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
