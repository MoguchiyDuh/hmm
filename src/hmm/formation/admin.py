from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import AuditAction, ErrorCode, TeamStatus
from hmm.formation import schemas
from hmm.formation.common import (
    audit,
    eligible_user,
    formation_sizes,
    locked_team,
    members,
    membership_row,
    set_team_status,
    team_view,
)
from hmm.formation.teams import assign_lead, place_member, remove_member
from hmm.notifications.payloads import TeamDisbandedPayload
from hmm.notifications.service import notify_many
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import ReasonBody
from hmm.platform.settings import load_settings


async def admin_add_member(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.InviteBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, formation = await locked_team(session, project_id)
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
    await place_member(
        session,
        project,
        event,
        formation,
        body.user_id,
        now,
        source="self",
        joined_via="admin",
        actor=actor,
    )
    await audit(
        session, project_id, now, "member_added", actor, AuditAction.MEMBER_ADD, body.reason
    )
    return await team_view(session, project)


async def admin_remove_member(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    actor: models.User,
    now: datetime,
    reason: str | None = None,
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id)
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
    project, event, _ = await locked_team(session, project_id)
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
    project, event, _ = await locked_team(session, project_id)
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
    set_team_status(project, TeamStatus.DISBANDED)
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
