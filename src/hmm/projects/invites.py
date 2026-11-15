from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode, InviteKind, JoinedVia
from hmm.formation.schemas import Invite
from hmm.identity.service import require_eligible
from hmm.notifications.payloads import (
    InviteAcceptedPayload,
    InviteDeclinedPayload,
    NotificationPerson,
    ProjectInviteReceivedPayload,
    ProjectMemberJoinedPayload,
)
from hmm.notifications.service import notify, notify_many
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import notification_person
from hmm.platform.settings import load_settings
from hmm.projects import schemas
from hmm.projects.common import project_member_ids
from hmm.projects.views import CLOSED_PROJECT_STATUSES, POST_RESULT_TEAM_STATUSES


async def add_member(
    session: AsyncSession, project: models.Project, user_id: UUID, via: JoinedVia, now: datetime
) -> None:
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    ids = await project_member_ids(session, project.id)
    if user_id in ids:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if len(ids) >= (await load_settings(session)).project_size_max:
        raise ProblemError(ErrorCode.PROJECT_FULL)
    row = models.ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role="member",
        state="confirmed",
        trust="self",
        joined_via=via.value,
        is_team_row=project.team_status is not None,
        joined_at=now,
        confirmed_at=now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(project)
    user = await session.get(models.User, user_id)
    if user is not None:
        await notify_many(
            session,
            ids,
            ProjectMemberJoinedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=notification_person(user),
                member_count=len(ids) + 1,
            ),
            now,
            dedupe_key=f"project_member_joined:{row.id}",
        )


async def invite_view(session: AsyncSession, row: models.Invite) -> Invite:
    await session.flush()
    await session.refresh(row)
    return Invite.model_validate(row, from_attributes=True)


async def create_invite(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> Invite:
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project.status in CLOSED_PROJECT_STATUSES or (
        project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES
    ):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    user = await require_eligible(session, body.user_id, now, invite=True)
    if user.id in await project_member_ids(session, project.id):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if (
        len(await project_member_ids(session, project.id))
        >= (await load_settings(session)).project_size_max
    ):
        raise ProblemError(ErrorCode.PROJECT_FULL)
    existing = await session.scalar(
        select(models.Invite).where(
            models.Invite.project_id == project.id,
            models.Invite.user_id == user.id,
            models.Invite.status == "pending",
        )
    )
    if existing is not None:
        if existing.expires_at > now:
            raise ProblemError(ErrorCode.ALREADY_INVITED)
        existing.status = "expired"
        existing.responded_at = now
        await session.flush()
    area_id = None
    if body.area_key is not None:
        area_id = await session.scalar(
            select(models.Area.id).where(
                models.Area.key == body.area_key, models.Area.archived_at.is_(None)
            )
        )
        if area_id is None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR)
    row = models.Invite(
        project_id=project.id,
        user_id=user.id,
        invited_by=actor.id,
        kind="team",
        status="pending",
        message=body.message,
        area_id=area_id,
        expires_at=now + timedelta(days=(await load_settings(session)).invite_ttl_days),
    )
    session.add(row)
    await session.flush()
    await session.refresh(project)
    await notify(
        session,
        user.id,
        ProjectInviteReceivedPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            invite_id=row.id,
            actor=notification_person(actor),
            message=body.message,
            area_key=body.area_key,
            expires_at=row.expires_at,
        ),
        now,
        dedupe_key=f"project_invite:{row.id}",
    )
    return await invite_view(session, row)


async def respond_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime, action: str
) -> Invite | ProblemError:
    row = await session.get(models.Invite, invite_id)
    if row is None or row.event_id is not None or row.project_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await get_or_404(session, models.Project, row.project_id, lock=True)
    await session.refresh(row)
    if action == "withdrawn":
        if row.invited_by != actor.id:
            raise ProblemError(ErrorCode.FORBIDDEN)
    elif row.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == action:
        return await invite_view(session, row)
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if row.expires_at <= now:
        row.status = "expired"
        row.responded_at = now
        await session.flush()
        return ProblemError(ErrorCode.INVALID_STATE, "Invite has expired")
    if action == "accepted":
        await require_eligible(session, actor.id, now, invite=True)
        await add_member(session, project, actor.id, JoinedVia.INVITE, now)
    row.status = action
    row.responded_at = now
    if row.invited_by is not None and action in ("accepted", "declined"):
        inviter = await session.get(models.User, row.invited_by)
        payload = InviteAcceptedPayload if action == "accepted" else InviteDeclinedPayload
        await notify(
            session,
            row.invited_by,
            payload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                invite_id=row.id,
                kind=InviteKind.TEAM,
                actor=NotificationPerson(
                    user_id=inviter.id if inviter is not None else actor.id,
                    display_name=inviter.display_name
                    if inviter is not None
                    else actor.display_name,
                ),
                invitee=notification_person(actor),
                expires_at=row.expires_at,
            ),
            now,
            dedupe_key=f"invite_{action}:{row.id}",
        )
    return await invite_view(session, row)
