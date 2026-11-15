from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    MemberRole,
    MemberState,
    ProjectStatus,
)
from hmm.notifications.payloads import LeadAssignedPayload, NotificationPerson
from hmm.notifications.service import notify
from hmm.platform.audit import record_audit


async def assign_project_lead(
    session: AsyncSession,
    project: models.Project,
    member: models.ProjectMember,
    now: datetime,
    actor: models.User | None = None,
) -> None:
    previous = project.lead_user_id
    if previous == member.user_id:
        return None
    for row in await session.scalars(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
        )
    ):
        row.role = MemberRole.LEAD.value if row.id == member.id else MemberRole.MEMBER.value
    project.lead_user_id = member.user_id
    await session.flush()
    await session.refresh(project)
    user = await session.get(models.User, member.user_id)
    if user is not None:
        await notify(
            session,
            user.id,
            LeadAssignedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                team_name=project.name,
                lead=NotificationPerson(user_id=user.id, display_name=user.display_name),
                reason="Project lead assigned",
            ),
            now,
            dedupe_key=f"lead_assigned:{project.id}:{previous}:{user.id}:{now.isoformat()}",
        )
    await record_audit(
        session,
        actor=actor.id if actor is not None else None,
        actor_kind=ActorKind.ADMIN
        if actor is not None and actor.is_admin
        else ActorKind.USER
        if actor is not None
        else ActorKind.SYSTEM,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": str(previous) if previous else None},
        after={"lead_user_id": str(member.user_id)},
        reason=None,
    )


async def standalone_lead_succession(
    session: AsyncSession, project: models.Project, now: datetime, *, exclude: set[UUID]
) -> None:
    await session.flush()
    member = await session.scalar(
        select(models.ProjectMember)
        .join(models.User, models.User.id == models.ProjectMember.user_id)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.state == MemberState.CONFIRMED.value,
            models.ProjectMember.user_id.not_in(exclude),
            models.User.banned_at.is_(None),
        )
        .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        .limit(1)
    )
    if member is not None:
        await assign_project_lead(session, project, member, now)
    else:
        project.lead_user_id = None
        project.status = ProjectStatus.PAUSED.value
        await session.flush()
        await session.refresh(project)
