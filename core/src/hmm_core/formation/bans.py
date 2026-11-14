from datetime import datetime
from typing import cast
from uuid import UUID

from hmm_contracts.common import ActorKind, LeadOfferStatus, LeftReason, TeamStatus
from hmm_contracts.notifications import AlertKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import REMOVABLE_TEAM_STATUSES
from hmm_core.formation.lead_offers import _retire_lead_offer, team_member_ids
from hmm_core.formation.members import remove_member_from_team
from hmm_core.notifications.service import raise_alert
from hmm_core.platform.settings import load_settings
from hmm_core.projects import service as projects


async def remove_user_from_teams(
    session: AsyncSession, user_id: UUID, now: datetime, *, actor: UUID
) -> list[UUID]:
    rows = (
        await session.execute(
            select(models.Project, models.ProjectMember)
            .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
            .where(
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.Project.team_status.is_not(None),
            )
            .order_by(models.Project.id)
        )
    ).all()
    offered = (
        await session.execute(
            select(models.LeadOffer.id, models.Project.id, models.Project.event_id)
            .join(models.Project, models.Project.id == models.LeadOffer.project_id)
            .where(
                models.LeadOffer.user_id == user_id,
                models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            )
            .order_by(models.LeadOffer.id)
        )
    ).all()
    if not rows and not offered:
        return []
    removable = [
        (project, member)
        for project, member in rows
        if project.team_status in REMOVABLE_TEAM_STATUSES and project.event_id is not None
    ]
    kept = [project for project, _ in rows if project.team_status == TeamStatus.ACTIVE.value]
    events: dict[UUID, models.Event] = {}
    locked: dict[UUID, models.Project] = {}
    project_ids = sorted(
        {project.id for project, _ in removable} | {project_id for _, project_id, _ in offered}
    )
    event_ids = sorted(
        {cast(UUID, project.event_id) for project, _ in removable}
        | {event_id for _, _, event_id in offered if event_id is not None}
    )
    if event_ids:
        await session.execute(
            select(models.Event.id)
            .where(models.Event.id.in_(event_ids))
            .order_by(models.Event.id)
            .with_for_update()
        )
        for event_id in event_ids:
            events[event_id] = await session.get_one(models.Event, event_id)
    if project_ids:
        locked = {
            project.id: project
            for project in await session.scalars(
                select(models.Project)
                .where(models.Project.id.in_(project_ids))
                .order_by(models.Project.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
    offers = (
        list(
            await session.scalars(
                select(models.LeadOffer)
                .where(models.LeadOffer.id.in_([offer_id for offer_id, _, _ in offered]))
                .order_by(models.LeadOffer.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if offered
        else []
    )
    settings = await load_settings(session)
    for stale, member in removable:
        project = locked[stale.id]
        current = await session.scalar(
            select(models.ProjectMember)
            .where(models.ProjectMember.id == member.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None or current.left_at is not None:
            continue
        event = events[cast(UUID, project.event_id)]
        min_size = event.team_size_min or settings.team_size_default_min
        await remove_member_from_team(
            session,
            project,
            event,
            current,
            now,
            left_reason=LeftReason.BANNED,
            notice_reason="The member left the team",
            audit_actor=actor,
            audit_kind=ActorKind.ADMIN,
        )
        size = len(await team_member_ids(session, project.id))
        if project.team_status in (TeamStatus.REVIEW.value, TeamStatus.LOCKED.value) and (
            size < min_size
        ):
            await raise_alert(
                session,
                AlertKind.TEAM_BELOW_MIN,
                now,
                title=f"Team below minimum: {project.name}",
                message=(
                    f"{project.name} has {size} members after a member was banned"
                    f" (minimum {min_size})"
                ),
                link=f"/admin/projects/{project.id}",
                subject_type="project",
                subject_id=project.id,
                subject_title=project.name,
                dedupe_key=f"team_below_min:{project.id}",
            )
    if kept:
        user = await session.get(models.User, user_id)
        if user is not None:
            for project in kept:
                await raise_alert(
                    session,
                    AlertKind.BANNED_MEMBER_IN_ACTIVE_TEAM,
                    now,
                    title=f"Banned member on an active team: {project.name}",
                    message=(
                        f"{user.display_name} was banned while on the active team {project.name}"
                    ),
                    link=f"/admin/projects/{project.id}",
                    subject_type="project",
                    subject_id=project.id,
                    subject_title=project.name,
                    dedupe_key=f"banned_member_in_active_team:{project.id}:{user_id}",
                )
    for offer in offers:
        await session.refresh(offer)
        project = locked[offer.project_id]
        if offer.status != LeadOfferStatus.PENDING.value or projects.lead_changes_closed(project):
            continue
        await _retire_lead_offer(
            session,
            offer,
            project,
            events.get(project.event_id) if project.event_id is not None else None,
            now,
            status=LeadOfferStatus.CLOSED,
            reason="The offered member is no longer available",
        )
    await session.flush()
    return sorted(project.id for project, _ in removable)
