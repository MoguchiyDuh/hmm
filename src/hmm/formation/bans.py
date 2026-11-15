from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.formation.common import detach_party, participation_event, set_enrollment_status
from hmm.formation.teams import reassign_lead, remove_member
from hmm.notifications.payloads import AlertKind
from hmm.notifications.service import raise_alert
from hmm.projects.invites import close_invite


async def remove_user_from_formation(
    session: AsyncSession, user_id: UUID, now: datetime, *, actor: models.User | None = None
) -> None:
    enrollment_events = select(models.Enrollment.event_id).where(
        models.Enrollment.user_id == user_id,
        models.Enrollment.status.in_(["active", "reserve", "placed"]),
    )
    team_events = select(models.ProjectMember.event_id).where(
        models.ProjectMember.user_id == user_id,
        models.ProjectMember.is_team_row,
        models.ProjectMember.left_at.is_(None),
    )
    invite_events = select(models.Invite.event_id).where(
        (models.Invite.user_id == user_id) | (models.Invite.invited_by == user_id),
        models.Invite.status == "pending",
        models.Invite.event_id.is_not(None),
    )
    event_ids = sorted(
        {
            event_id
            for event_id in await session.scalars(
                enrollment_events.union(team_events, invite_events)
            )
            if event_id is not None
        }
    )
    events = [await participation_event(session, event_id) for event_id in event_ids]
    for event, _ in events:
        for row in await session.scalars(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.user_id == user_id,
                models.Enrollment.status.in_(["active", "reserve", "placed"]),
            )
        ):
            await detach_party(session, row, now)
            await set_enrollment_status(session, row, "withdrawn", now)
        for invite in await session.scalars(
            select(models.Invite).where(
                models.Invite.event_id == event.id,
                (models.Invite.user_id == user_id) | (models.Invite.invited_by == user_id),
                models.Invite.status == "pending",
            )
        ):
            await close_invite(session, invite, event, now, "closed", "banned")
        for member in await session.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.event_id == event.id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.is_team_row,
                models.ProjectMember.left_at.is_(None),
            )
        ):
            project = await session.get(models.Project, member.project_id)
            if project is None:
                continue
            if project.team_status == "formed":
                await remove_member(
                    session, project, event, member, now, "withdrawn", "banned", actor
                )
            else:
                await reassign_lead(session, project, event, now, actor)
                if project.team_status == "active":
                    await raise_alert(
                        session,
                        AlertKind.BANNED_MEMBER_IN_ACTIVE_TEAM,
                        now,
                        title="Banned member in active team",
                        message="Member retained for history",
                        link=f"/projects/{project.id}",
                        subject_type="project",
                        subject_id=project.id,
                        subject_title=project.name,
                        dedupe_key=f"banned_member_in_active_team:{project.id}:{user_id}",
                    )
    await session.flush()
