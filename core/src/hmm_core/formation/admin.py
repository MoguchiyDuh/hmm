from datetime import datetime
from uuid import UUID

from hmm_contracts.common import AuditAction, ErrorCode
from hmm_contracts.notifications import TeamDisbandedPayload
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    audit,
    eligible_user,
    formation_sizes,
    locked_team,
    members,
    membership_row,
    set_enrollment_status,
    team_view,
)
from hmm_core.formation.parties import detach_party
from hmm_core.formation.queue import queue_user
from hmm_core.formation.teams import assign_lead, reassign_lead, remove_member
from hmm_core.notifications.service import notify_many, resolve_alerts
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.settings import load_settings


async def admin_add_member(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.InviteBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, formation = await locked_team(session, project_id, actor)
    rows = await members(session, project_id)
    existing = next((row for row in rows if row.user_id == body.user_id), None)
    if existing is not None:
        if existing.state != "confirmed":
            existing.state = "confirmed"
            existing.confirmed_at = now
            existing.accept_by = None
            await audit(
                session,
                project_id,
                now,
                "member_confirmed",
                actor,
                AuditAction.MEMBER_ADD,
                body.reason,
            )
        return await team_view(session, project)
    if project.team_status not in {"formed", "active"}:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await eligible_user(session, body.user_id, event.id, now)
    _, _, maximum = formation_sizes(event, formation, await load_settings(session))
    if len(rows) >= maximum:
        raise ProblemError(ErrorCode.TEAM_FULL)
    enrollment = await queue_user(session, event, formation, body.user_id, now)
    await detach_party(session, enrollment, now)
    session.add(
        models.ProjectMember(
            project_id=project_id,
            event_id=event.id,
            user_id=body.user_id,
            role="member",
            state="confirmed",
            trust="self",
            joined_via="admin",
            is_team_row=True,
            joined_at=now,
            confirmed_at=now,
        )
    )
    await set_enrollment_status(session, enrollment, "placed", now)
    await reassign_lead(session, project, event, now, actor)
    await audit(
        session, project_id, now, "member_added", actor, AuditAction.MEMBER_ADD, body.reason
    )
    _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
    if len(await members(session, project_id)) >= minimum:
        await resolve_alerts(session, f"team_below_min:{project_id}", now)
    return await team_view(session, project)


async def admin_remove_member(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    actor: models.User,
    now: datetime,
    reason: str | None = None,
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    row = await membership_row(session, project_id, user_id)
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.left_at is not None:
        if row.left_reason == "admin":
            return await team_view(session, project)
        raise ProblemError(ErrorCode.INVALID_STATE)
    await remove_member(
        session,
        project,
        event,
        row,
        now,
        "reserve" if now < event.starts_at else "withdrawn",
        "admin",
        actor,
        audit_reason=reason,
    )
    return await team_view(session, project)


async def admin_set_lead(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.TransferLeadBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    if not any(row.user_id == body.user_id for row in await members(session, project_id)):
        raise ProblemError(ErrorCode.INVALID_STATE)
    user = await session.get(models.User, body.user_id)
    if user is None or user.banned_at is not None:
        raise ProblemError(ErrorCode.BANNED)
    await assign_lead(
        session, project, event, body.user_id, now, actor, body.reason or "Admin assigned lead"
    )
    return await team_view(session, project)


async def admin_disband(
    session: AsyncSession, project_id: UUID, body: ReasonBody, actor: models.User, now: datetime
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    if project.team_status == "disbanded":
        return await team_view(session, project)
    if project.team_status != "formed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    rows = await members(session, project_id)
    for row in rows:
        await remove_member(
            session,
            project,
            event,
            row,
            now,
            "reserve" if now < event.starts_at else "withdrawn",
            "disbanded",
            actor,
        )
    project.team_status = "disbanded"
    project.lead_user_id = None
    await audit(
        session, project_id, now, "disbanded", actor, AuditAction.PROJECT_DISBAND, body.reason
    )
    await notify_many(
        session,
        [row.user_id for row in rows],
        TeamDisbandedPayload(
            link=f"/projects/{project_id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project_id,
            team_name=project.name,
            reason=body.reason or "Admin disbanded team",
        ),
        now,
        dedupe_key=f"team_disbanded:{project_id}",
    )
    return await team_view(session, project)
