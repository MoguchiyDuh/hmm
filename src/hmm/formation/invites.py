from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode, InviteKind
from hmm.formation import schemas
from hmm.formation.common import (
    detach_party,
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
    set_enrollment_status,
)
from hmm.formation.parties import locked_party
from hmm.formation.queue import queue_user
from hmm.formation.teams import reassign_lead
from hmm.notifications.payloads import (
    ActionKey,
    ApiAction,
    InviteAcceptedPayload,
    InviteClosedPayload,
    InviteDeclinedPayload,
    InviteExpiredPayload,
    PartyInvitePayload,
    TeamInvitePayload,
    TeamMemberJoinedPayload,
)
from hmm.notifications.service import notify, notify_many, resolve_alerts
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import notification_person
from hmm.platform.settings import load_settings


async def invite_view(session: AsyncSession, row: models.Invite) -> schemas.Invite:
    await session.flush()
    await session.refresh(row)
    return schemas.Invite.model_validate(row, from_attributes=True)


async def create_party_invite(
    session: AsyncSession,
    party_id: UUID,
    body: schemas.InviteBody,
    actor: models.User,
    now: datetime,
) -> schemas.Invite:
    _, event, _ = await locked_party(session, party_id, now)
    if actor.id not in {row.user_id for row in await party_rows(session, party_id)}:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if body.user_id in {row.user_id for row in await party_rows(session, party_id)}:
        raise ProblemError(ErrorCode.INVALID_STATE)
    user = await eligible_user(session, body.user_id, event.id, now, invite=True)
    existing = await session.scalar(
        select(models.Invite).where(
            models.Invite.party_id == party_id,
            models.Invite.user_id == body.user_id,
            models.Invite.status == "pending",
        )
    )
    if existing is not None:
        return await invite_view(session, existing)
    settings = await load_settings(session)
    row = models.Invite(
        kind="party",
        party_id=party_id,
        event_id=event.id,
        user_id=user.id,
        invited_by=actor.id,
        status="pending",
        expires_at=min(now + timedelta(days=settings.invite_ttl_days), reg_deadline(event)),
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


async def create_invite(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.InviteBody,
    actor: models.User,
    now: datetime,
) -> schemas.Invite:
    project, event, formation = await locked_team(session, project_id)
    require_lead(project, actor)
    queue_open(event, formation, now)
    if project.team_status != "formed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    user = await eligible_user(session, body.user_id, event.id, now, invite=True)
    settings = await load_settings(session)
    _, _, maximum = formation_sizes(event, formation, settings)
    if len(await members(session, project_id)) >= maximum:
        raise ProblemError(ErrorCode.TEAM_FULL)
    existing = await session.scalar(
        select(models.Invite).where(
            models.Invite.project_id == project_id,
            models.Invite.user_id == user.id,
            models.Invite.status == "pending",
        )
    )
    if existing is not None:
        return await invite_view(session, existing)
    row = models.Invite(
        kind="team",
        project_id=project_id,
        event_id=event.id,
        user_id=user.id,
        invited_by=actor.id,
        status="pending",
        expires_at=min(now + timedelta(days=settings.invite_ttl_days), reg_deadline(event)),
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


async def invite_outcome(
    session: AsyncSession, row: models.Invite, event: models.Event, now: datetime, reason: str = ""
) -> None:
    user = await session.get(models.User, row.user_id)
    inviter = await session.get(models.User, row.invited_by) if row.invited_by else None
    if user is None:
        return None
    project = await session.get(models.Project, row.project_id) if row.project_id else None
    payload_type = {
        "accepted": InviteAcceptedPayload,
        "declined": InviteDeclinedPayload,
        "expired": InviteExpiredPayload,
        "closed": InviteClosedPayload,
    }.get(row.status)
    if payload_type is None:
        return None
    data = {
        "link": f"/events/{event.id}/formation",
        "invite_id": row.id,
        "kind": InviteKind(row.kind),
        "party_id": row.party_id,
        "project_id": row.project_id,
        "project_name": project.name if project else None,
        "actor": notification_person(inviter or user),
        "invitee": notification_person(user),
        "expires_at": row.expires_at,
        "event_id": event.id,
        "event_title": event.title,
    }
    if row.status == "closed":
        data["reason"] = reason
    payload = payload_type.model_validate(data)
    recipients = [row.user_id] if row.status in {"expired", "closed"} else []
    if inviter:
        recipients.append(inviter.id)
    await notify_many(session, recipients, payload, now, dedupe_key=f"invite_{row.status}:{row.id}")


async def close_invite(
    session: AsyncSession,
    row: models.Invite,
    event: models.Event,
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


async def locked_invite(
    session: AsyncSession, invite_id: UUID
) -> tuple[models.Invite, models.Event, models.EventFormation]:
    row = await session.get(models.Invite, invite_id)
    if row is None or row.event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event, formation = await participation_event(session, row.event_id)
    await session.refresh(row)
    return row, event, formation


async def accept_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> schemas.Invite | ProblemError:
    row, event, formation = await locked_invite(session, invite_id)
    if row.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == "accepted":
        return await invite_view(session, row)
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if now >= row.expires_at or now >= reg_deadline(event):
        await close_invite(session, row, event, now, "expired")
        return ProblemError(ErrorCode.INVALID_STATE, "Invite has expired")
    queue_open(event, formation, now)
    await eligible_user(session, actor.id, event.id, now)
    _, minimum, maximum = formation_sizes(event, formation, await load_settings(session))
    if row.party_id is not None:
        party = await get_or_404(session, models.Party, row.party_id)
        enrollment = await live_enrollment_row(session, event.id, actor.id)
        if enrollment is None or enrollment.party_id != party.id:
            if len(await party_rows(session, party.id)) >= maximum:
                raise ProblemError(ErrorCode.PARTY_FULL)
            enrollment = await queue_user(session, event, formation, actor.id, now, "invite")
            await detach_party(session, enrollment, now)
            enrollment.party_id = party.id
    elif row.project_id is not None:
        project = await session.get(models.Project, row.project_id)
        if project is None or project.team_status != "formed":
            raise ProblemError(ErrorCode.INVALID_STATE)
        if len(await members(session, project.id)) >= maximum:
            raise ProblemError(ErrorCode.TEAM_FULL)
        enrollment = await queue_user(session, event, formation, actor.id, now, "invite")
        await detach_party(session, enrollment, now)
        session.add(
            models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=actor.id,
                role="member",
                state="confirmed",
                trust="self",
                joined_via="invite",
                is_team_row=True,
                joined_at=now,
                confirmed_at=now,
                area_id=row.area_id,
            )
        )
        await set_enrollment_status(session, enrollment, "placed", now)
        await reassign_lead(session, project, event, now)
        if len(await members(session, project.id)) >= minimum:
            await resolve_alerts(session, f"team_below_min:{project.id}", now)
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
    await close_invite(session, row, event, now, "accepted")
    return await invite_view(session, row)


async def decline_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> schemas.Invite | ProblemError:
    row, event, _ = await locked_invite(session, invite_id)
    if row.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == "declined":
        return await invite_view(session, row)
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if now >= row.expires_at or now >= reg_deadline(event):
        await close_invite(session, row, event, now, "expired")
        return ProblemError(ErrorCode.INVALID_STATE, "Invite has expired")
    await close_invite(session, row, event, now, "declined")
    return await invite_view(session, row)


async def withdraw_invite(
    session: AsyncSession, invite_id: UUID, actor: models.User, now: datetime
) -> ProblemError | None:
    row, event, _ = await locked_invite(session, invite_id)
    if row.invited_by != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if row.status == "withdrawn":
        return None
    if row.status != "pending":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if now >= row.expires_at or now >= reg_deadline(event):
        await close_invite(session, row, event, now, "expired")
        return ProblemError(ErrorCode.INVALID_STATE, "Invite has expired")
    await close_invite(session, row, event, now, "withdrawn")


async def expire_invites(session: AsyncSession, event: models.Event, now: datetime) -> None:
    rows = await session.scalars(
        select(models.Invite).where(
            models.Invite.event_id == event.id, models.Invite.status == "pending"
        )
    )
    for row in rows:
        if now >= row.expires_at or now >= reg_deadline(event):
            await close_invite(session, row, event, now, "expired")


async def my_invites(
    session: AsyncSession, actor: models.User, status: str | None = None
) -> list[schemas.Invite]:
    query = select(models.Invite).where(models.Invite.user_id == actor.id)
    if status:
        query = query.where(models.Invite.status == status)
    return [
        schemas.Invite.model_validate(row, from_attributes=True)
        for row in await session.scalars(
            query.order_by(models.Invite.created_at.desc(), models.Invite.id.desc())
        )
    ]
