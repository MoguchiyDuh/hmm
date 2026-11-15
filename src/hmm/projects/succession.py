from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectStatus,
)
from hmm.notifications.payloads import LeadAssignedPayload, ProjectMemberLeftPayload
from hmm.notifications.service import notify, notify_many
from hmm.platform.audit import record_audit
from hmm.platform.schemas import notification_person
from hmm.projects.common import project_member_ids


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
                lead=notification_person(user),
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


async def depart_member(
    session: AsyncSession,
    project: models.Project,
    member: models.ProjectMember,
    user: models.User,
    reason: LeftReason,
    now: datetime,
    *,
    actor: UUID,
    actor_kind: ActorKind,
) -> None:
    was_lead = project.lead_user_id == member.user_id
    member.left_at = now
    member.left_reason = reason.value
    if was_lead:
        project.lead_user_id = None
        await record_audit(
            session,
            actor=actor,
            actor_kind=actor_kind,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(member.user_id)},
            after={"lead_user_id": None},
            reason="banned" if reason == LeftReason.BANNED else None,
        )
    if project.lead_user_id is None:
        await standalone_lead_succession(session, project, now, exclude={member.user_id})
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
            member=notification_person(user),
            member_count=len(remaining),
        ),
        now,
        dedupe_key=f"project_member_left:{member.id}",
    )
