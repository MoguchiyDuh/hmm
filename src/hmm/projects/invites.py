from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import procrastinate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode, InviteKind, JoinedVia
from hmm.formation import schemas as formation_schemas
from hmm.formation.common import detach_party as detach_from_party
from hmm.formation.common import (
    eligible_user,
    formation_sizes,
    live_enrollment_row,
    locked_team,
    members,
    participation_event,
    party_rows,
    queue_open,
    reg_deadline,
    require_lead,
)
from hmm.formation.parties import locked_party
from hmm.formation.queue import queue_user
from hmm.formation.teams import place_member
from hmm.identity.service import require_eligible
from hmm.notifications.payloads import (
    ActionKey,
    ApiAction,
    InviteAcceptedPayload,
    InviteClosedPayload,
    InviteDeclinedPayload,
    InviteExpiredPayload,
    PartyInvitePayload,
    ProjectInviteReceivedPayload,
    TeamInvitePayload,
    TeamMemberJoinedPayload,
)
from hmm.notifications.service import notify, notify_many
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import notification_person
from hmm.platform.settings import load_settings
from hmm.projects import schemas
from hmm.projects.common import project_member_ids
from hmm.projects.members import add_member
from hmm.projects.views import CLOSED_PROJECT_STATUSES, POST_RESULT_TEAM_STATUSES
from hmm.recommendations import service as recommendations

type InviteResponse = Literal["accepted", "declined", "withdrawn"]

OUTCOME_PAYLOADS = {
    "accepted": InviteAcceptedPayload,
    "declined": InviteDeclinedPayload,
    "expired": InviteExpiredPayload,
    "closed": InviteClosedPayload,
}


async def invite_view(session: AsyncSession, row: models.Invite) -> formation_schemas.Invite:
    await session.flush()
    await session.refresh(row)
    return formation_schemas.Invite.model_validate(row, from_attributes=True)


def invite_actions(invite_id: UUID) -> tuple[ApiAction, ...]:
    return (
        ApiAction(
            key=ActionKey.INVITE_ACCEPT,
            entity_id=invite_id,
            label_key="notifications.actions.accept",
        ),
        ApiAction(
            key=ActionKey.INVITE_DECLINE,
            entity_id=invite_id,
            label_key="notifications.actions.decline",
        ),
    )


def invite_expired(row: models.Invite, event: models.Event | None, now: datetime) -> bool:
    return now >= row.expires_at or (event is not None and now >= reg_deadline(event))


async def invite_outcome(
    session: AsyncSession,
    row: models.Invite,
    event: models.Event | None,
    now: datetime,
    reason: str = "",
) -> None:
    payload_type = OUTCOME_PAYLOADS.get(row.status)
    user = await session.get(models.User, row.user_id)
    if payload_type is None or user is None:
        return None
    inviter = await session.get(models.User, row.invited_by) if row.invited_by else None
    project = await session.get(models.Project, row.project_id) if row.project_id else None
    data = {
        "link": f"/events/{event.id}/formation" if event else f"/projects/{row.project_id}",
        "invite_id": row.id,
        "kind": InviteKind(row.kind),
        "party_id": row.party_id,
        "project_id": row.project_id,
        "project_name": project.name if project else None,
        "actor": notification_person(inviter or user),
        "invitee": notification_person(user),
        "expires_at": row.expires_at,
        "event_id": event.id if event else None,
        "event_title": event.title if event else None,
    }
    if row.status == "closed":
        data["reason"] = reason
    recipients = [row.user_id] if row.status in {"expired", "closed"} else []
    if inviter:
        recipients.append(inviter.id)
    await notify_many(
        session,
        recipients,
        payload_type.model_validate(data),
        now,
        dedupe_key=f"invite_{row.status}:{row.id}",
    )


async def close_invite(
    session: AsyncSession,
    row: models.Invite,
    event: models.Event | None,
    now: datetime,
    status: str,
    reason: str = "",
) -> None:
    if row.status != "pending":
        return None
    row.status = status
    row.responded_at = now
    row.closed_reason = reason or None
    await session.flush()
    await invite_outcome(session, row, event, now, reason)


async def pending_invite(
    session: AsyncSession,
    user_id: UUID,
    event: models.Event | None,
    now: datetime,
    *,
    party_id: UUID | None = None,
    project_id: UUID | None = None,
) -> models.Invite | None:
    target = (
        models.Invite.party_id == party_id
        if party_id is not None
        else models.Invite.project_id == project_id
    )
    row = await session.scalar(
        select(models.Invite).where(
            target, models.Invite.user_id == user_id, models.Invite.status == "pending"
        )
    )
    if row is not None and invite_expired(row, event, now):
        await close_invite(session, row, event, now, "expired")
        return None
    return row


async def invite_expiry(
    session: AsyncSession, event: models.Event | None, now: datetime
) -> datetime:
    expires_at = now + timedelta(days=(await load_settings(session)).invite_ttl_days)
    return expires_at if event is None else min(expires_at, reg_deadline(event))


async def invite_area_id(session: AsyncSession, key: str | None) -> UUID | None:
    if key is None:
        return None
    area_id = await session.scalar(
        select(models.Area.id).where(models.Area.key == key, models.Area.archived_at.is_(None))
    )
    if area_id is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR)
    return area_id


async def create_party_invite(
    session: AsyncSession,
    party_id: UUID,
    body: formation_schemas.InviteBody,
    actor: models.User,
    now: datetime,
) -> formation_schemas.Invite:
    _, event, _ = await locked_party(session, party_id, now)
    party_members = {row.user_id for row in await party_rows(session, party_id)}
    if actor.id not in party_members:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if body.user_id in party_members:
        raise ProblemError(ErrorCode.INVALID_STATE)
    user = await eligible_user(session, body.user_id, event.id, now, invite=True)
    existing = await pending_invite(session, user.id, event, now, party_id=party_id)
    if existing is not None:
        return await invite_view(session, existing)
    row = models.Invite(
        kind="party",
        party_id=party_id,
        event_id=event.id,
        user_id=user.id,
        invited_by=actor.id,
        status="pending",
        expires_at=await invite_expiry(session, event, now),
    )
    session.add(row)
    await session.flush()
    await notify(
        session,
        user.id,
        PartyInvitePayload(
            link=f"/parties/{party_id}",
            party_id=party_id,
            event_id=event.id,
            event_title=event.title,
            invite_id=row.id,
            actor=notification_person(actor),
            invitee=notification_person(user),
            expires_at=row.expires_at,
            actions=invite_actions(row.id),
        ),
        now,
        dedupe_key=f"party_invite:{row.id}",
    )
    return await invite_view(session, row)


async def create_invite(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> formation_schemas.Invite:
    project = await get_or_404(session, models.Project, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        return await create_team_invite(session, project_id, body, actor, now)
    invite = await create_project_invite(session, project_id, body, actor, now)
    await recommendations.enqueue_project_refresh(session, jobs, project_id)
    return invite


async def create_team_invite(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> formation_schemas.Invite:
    project, event, formation = await locked_team(session, project_id)
    require_lead(project, actor)
    queue_open(event, formation, now)
    if project.team_status != "formed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    user = await eligible_user(session, body.user_id, event.id, now, invite=True)
    _, _, maximum = formation_sizes(event, formation, await load_settings(session))
    if len(await members(session, project_id)) >= maximum:
        raise ProblemError(ErrorCode.TEAM_FULL)
    existing = await pending_invite(session, user.id, event, now, project_id=project_id)
    if existing is not None:
        return await invite_view(session, existing)
    row = models.Invite(
        kind="team",
        project_id=project_id,
        event_id=event.id,
        user_id=user.id,
        invited_by=actor.id,
        status="pending",
        message=body.message,
        area_id=await invite_area_id(session, body.area_key),
        expires_at=await invite_expiry(session, event, now),
    )
    session.add(row)
    await session.flush()
    await notify(
        session,
        user.id,
        TeamInvitePayload(
            link=f"/projects/{project_id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project_id,
            team_name=project.name,
            invite_id=row.id,
            actor=notification_person(actor),
            invitee=notification_person(user),
            expires_at=row.expires_at,
            actions=invite_actions(row.id),
        ),
        now,
        dedupe_key=f"team_invite:{row.id}",
    )
    return await invite_view(session, row)


async def create_project_invite(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateProjectInviteBody,
    actor: models.User,
    now: datetime,
) -> formation_schemas.Invite:
    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    user = await require_eligible(session, body.user_id, now, invite=True)
    member_ids = await project_member_ids(session, project.id)
    if user.id in member_ids:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if len(member_ids) >= (await load_settings(session)).project_size_max:
        raise ProblemError(ErrorCode.PROJECT_FULL)
    existing = await pending_invite(session, user.id, None, now, project_id=project.id)
    if existing is not None:
        return await invite_view(session, existing)
    row = models.Invite(
        kind="team",
        project_id=project.id,
        user_id=user.id,
        invited_by=actor.id,
        status="pending",
        message=body.message,
        area_id=await invite_area_id(session, body.area_key),
        expires_at=await invite_expiry(session, None, now),
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


async def join_event_target(
    session: AsyncSession,
    row: models.Invite,
    event: models.Event,
    formation: models.EventFormation,
    actor: models.User,
    now: datetime,
) -> None:
    queue_open(event, formation, now)
    await eligible_user(session, actor.id, event.id, now)
    _, _, maximum = formation_sizes(event, formation, await load_settings(session))
    if row.party_id is not None:
        party = await get_or_404(session, models.Party, row.party_id)
        enrollment = await live_enrollment_row(session, event.id, actor.id)
        if enrollment is None or enrollment.party_id != party.id:
            if len(await party_rows(session, party.id)) >= maximum:
                raise ProblemError(ErrorCode.PARTY_FULL)
            enrollment = await queue_user(session, event, formation, actor.id, now, "invite")
            await detach_from_party(session, enrollment, now)
            enrollment.party_id = party.id
        return None
    project = await session.get(models.Project, row.project_id) if row.project_id else None
    if project is None or project.team_status != "formed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if len(await members(session, project.id)) >= maximum:
        raise ProblemError(ErrorCode.TEAM_FULL)
    await place_member(
        session,
        project,
        event,
        formation,
        actor.id,
        now,
        source="invite",
        joined_via="invite",
        area_id=row.area_id,
    )
    if project.lead_user_id:
        await notify(
            session,
            project.lead_user_id,
            TeamMemberJoinedPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                member=notification_person(actor),
                member_count=len(await members(session, project.id)),
            ),
            now,
            dedupe_key=f"team_member_joined:invite:{row.id}",
        )


async def respond_invite(
    session: AsyncSession,
    invite_id: UUID,
    actor: models.User,
    now: datetime,
    action: InviteResponse,
) -> formation_schemas.Invite:
    row = await get_or_404(session, models.Invite, invite_id)
    event: models.Event | None = None
    formation: models.EventFormation | None = None
    project: models.Project | None = None
    if row.event_id is not None:
        event, formation = await participation_event(session, row.event_id)
    elif row.project_id is not None:
        project = await get_or_404(session, models.Project, row.project_id, lock=True)
    else:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await session.refresh(row)
    if (row.invited_by if action == "withdrawn" else row.user_id) != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == action:
        return await invite_view(session, row)
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if invite_expired(row, event, now):
        await close_invite(session, row, event, now, "expired")
        raise ProblemError(ErrorCode.INVALID_STATE, "Invite has expired", commit=True)
    if action == "accepted":
        if event is not None and formation is not None:
            await join_event_target(session, row, event, formation, actor, now)
        elif project is not None:
            await require_eligible(session, actor.id, now)
            await add_member(session, project, actor.id, JoinedVia.INVITE, now)
    await close_invite(session, row, event, now, action)
    return await invite_view(session, row)


async def expire_invites(session: AsyncSession, event: models.Event, now: datetime) -> None:
    rows = await session.scalars(
        select(models.Invite).where(
            models.Invite.event_id == event.id, models.Invite.status == "pending"
        )
    )
    for row in rows:
        if invite_expired(row, event, now):
            await close_invite(session, row, event, now, "expired")


async def my_invites(
    session: AsyncSession, actor: models.User, status: str | None = None
) -> list[formation_schemas.Invite]:
    query = select(models.Invite).where(models.Invite.user_id == actor.id)
    if status:
        query = query.where(models.Invite.status == status)
    return [
        formation_schemas.Invite.model_validate(row, from_attributes=True)
        for row in await session.scalars(
            query.order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
        )
    ]
