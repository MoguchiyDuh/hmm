from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.pack import OpenTeam, pack
from hmm.common import AuditAction, ErrorCode
from hmm.formation import schemas
from hmm.formation.common import (
    area_id,
    audit,
    formation_sizes,
    live_enrollment_row,
    locked_team,
    members,
    membership_row,
    pick_lead,
    queue_units,
    reg_deadline,
    require_lead,
    set_enrollment_status,
    team_view,
)
from hmm.notifications.payloads import (
    LeadAssignedPayload,
    PlacementLapsedPayload,
    TeamMemberJoinedPayload,
    TeamMemberLeftPayload,
)
from hmm.notifications.service import notify, notify_many, resolve_alerts
from hmm.platform.errors import ProblemError
from hmm.platform.schemas import notification_person
from hmm.platform.settings import load_settings


async def assign_lead(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    user_id: UUID | None,
    now: datetime,
    actor: models.User | None = None,
    reason: str = "Lead succession",
) -> None:
    if project.lead_user_id == user_id:
        return None
    previous = project.lead_user_id
    rows = await members(session, project.id)
    for row in rows:
        row.role = "member"
    await session.flush()
    project.lead_user_id = user_id
    for row in rows:
        if row.user_id == user_id:
            row.role = "lead"
    await session.flush()
    await audit(session, project.id, now, str(user_id), actor, AuditAction.LEAD_ASSIGN, reason)
    if user_id is not None:
        user = await session.get(models.User, user_id)
        if user is not None:
            await notify(
                session,
                user_id,
                LeadAssignedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    project_name=project.name,
                    team_name=project.name,
                    lead=notification_person(user),
                    reason=reason,
                ),
                now,
                dedupe_key=f"lead_assigned:{project.id}:{previous}:{user_id}:{now.isoformat()}",
            )


async def reassign_lead(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    now: datetime,
    actor: models.User | None = None,
) -> None:
    rows = await members(session, project.id)
    users = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_([row.user_id for row in rows]))
        )
    }
    eligible = [
        row for row in rows if row.user_id in users and users[row.user_id].banned_at is None
    ]
    if not rows:
        await assign_lead(session, project, event, None, now, actor)
        project.team_status = "disbanded"
        await audit(session, project.id, now, "disbanded", actor)
        await resolve_alerts(session, f"team_below_min:{project.id}", now)
        return None
    if project.lead_user_id in {row.user_id for row in eligible}:
        return None
    enrollment_rows = list(
        await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.user_id.in_([row.user_id for row in eligible]),
            )
            .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        )
    )
    latest: dict[UUID, models.Enrollment] = {}
    for row in enrollment_rows:
        latest.setdefault(row.user_id, row)
    confirmed = {row.user_id for row in eligible if row.state == "confirmed"}
    candidates = confirmed or {row.user_id for row in eligible}
    lead = await pick_lead(
        session, list(latest.values()), [], confirmed=candidates, project_id=project.id
    )
    if lead is None and eligible:
        lead = min(
            eligible, key=lambda row: (row.state != "confirmed", row.joined_at, row.id)
        ).user_id
    await assign_lead(session, project, event, lead, now, actor)


async def remove_member(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    member: models.ProjectMember,
    now: datetime,
    status: str,
    reason: str,
    actor: models.User | None = None,
    *,
    audit_reason: str | None = None,
) -> None:
    if member.left_at is not None:
        return None
    deadline = member.accept_by
    member.left_at = now
    member.left_reason = reason
    row = await live_enrollment_row(session, event.id, member.user_id)
    if row is not None:
        await set_enrollment_status(session, row, status, now)
    await session.flush()
    await reassign_lead(session, project, event, now, actor)
    await audit(
        session, member.id, now, status, actor, AuditAction.MEMBER_REMOVE, audit_reason or reason
    )
    remaining = await members(session, project.id)
    user = await session.get(models.User, member.user_id)
    if user is not None:
        await notify_many(
            session,
            [row.user_id for row in remaining],
            TeamMemberLeftPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                member=notification_person(user),
                member_count=len(remaining),
                reason=reason,
            ),
            now,
            dedupe_key=f"team_member_left:{member.id}",
        )
    if status == "lapsed" and deadline is not None:
        await notify(
            session,
            member.user_id,
            PlacementLapsedPayload(
                link=f"/events/{event.id}/formation",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                accept_by=deadline,
                reason="Placement was not accepted",
                reg_deadline=reg_deadline(event),
            ),
            now,
            dedupe_key=f"placement_lapsed:{member.id}",
        )


async def accept_placement(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Team | ProblemError:
    project, event, _ = await locked_team(session, project_id, actor)
    member = next(
        (row for row in await members(session, project_id) if row.user_id == actor.id), None
    )
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if member.state == "confirmed":
        return await team_view(session, project)
    if member.accept_by is None or now >= member.accept_by or now >= event.starts_at:
        await remove_member(session, project, event, member, now, "lapsed", "lapsed")
        return ProblemError(ErrorCode.INVALID_STATE, "Placement has expired")
    member.state = "confirmed"
    member.confirmed_at = now
    member.accept_by = None
    await session.flush()
    if project.lead_user_id is not None and project.lead_user_id != actor.id:
        await notify(
            session,
            project.lead_user_id,
            TeamMemberJoinedPayload(
                link=f"/projects/{project_id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project_id,
                team_name=project.name,
                member=notification_person(actor),
                member_count=len(await members(session, project_id)),
            ),
            now,
            dedupe_key=f"team_member_joined:{member.id}",
        )
    return await team_view(session, project)


async def decline_placement(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.DeclinePlacementBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team | ProblemError:
    project, event, _ = await locked_team(session, project_id, actor)
    row = await membership_row(session, project_id, actor.id)
    if row is not None and row.left_at is not None and row.left_reason == "declined":
        return await team_view(session, project)
    if row is None or row.left_at is not None or row.state != "proposed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    if row.accept_by is None or now >= row.accept_by or now >= event.starts_at:
        await remove_member(session, project, event, row, now, "lapsed", "lapsed")
        return ProblemError(ErrorCode.INVALID_STATE, "Placement has expired")
    await remove_member(
        session,
        project,
        event,
        row,
        now,
        "withdrawn" if body.withdraw else "reserve",
        "declined",
        actor,
    )
    return await team_view(session, project)


async def leave_team(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.DeclinePlacementBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    member = await membership_row(session, project_id, actor.id)
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if member.left_at is not None:
        if member.left_reason == "left":
            return await team_view(session, project)
        raise ProblemError(ErrorCode.INVALID_STATE)
    await remove_member(
        session,
        project,
        event,
        member,
        now,
        "withdrawn" if body.withdraw or now >= event.starts_at else "reserve",
        "left",
        actor,
    )
    return await team_view(session, project)


async def kick_member(
    session: AsyncSession, project_id: UUID, user_id: UUID, actor: models.User, now: datetime
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    require_lead(project, actor)
    row = await membership_row(session, project_id, user_id)
    if row is not None and row.left_at is not None and row.left_reason == "removed":
        return await team_view(session, project)
    if project.team_status != "formed" or user_id == actor.id:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.left_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await remove_member(session, project, event, row, now, "reserve", "removed", actor)
    return await team_view(session, project)


async def set_seats(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.ClosedBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, formation = await locked_team(session, project_id, actor)
    require_lead(project, actor)
    if project.team_status != "formed":
        raise ProblemError(ErrorCode.INVALID_STATE)
    _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
    if body.closed and len(await members(session, project_id)) < minimum:
        raise ProblemError(ErrorCode.SEATS_CLOSE_BELOW_MIN)
    project.seats_closed_at = (project.seats_closed_at or now) if body.closed else None
    await session.flush()
    return await team_view(session, project)


async def transfer_lead(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.TransferLeadBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, event, _ = await locked_team(session, project_id, actor)
    require_lead(project, actor)
    target = next(
        (
            row
            for row in await members(session, project_id)
            if row.user_id == body.user_id and row.state == "confirmed"
        ),
        None,
    )
    if target is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "New lead must be a confirmed member")
    user = await session.get(models.User, body.user_id)
    if user is None or user.banned_at is not None:
        raise ProblemError(ErrorCode.BANNED)
    await assign_lead(session, project, event, target.user_id, now, actor, "Lead transferred")
    return await team_view(session, project)


async def update_member_area(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    body: schemas.UpdateMemberBody,
    actor: models.User,
    now: datetime,
) -> schemas.Team:
    project, _, _ = await locked_team(session, project_id, actor)
    require_lead(project, actor)
    row = next((row for row in await members(session, project_id) if row.user_id == user_id), None)
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    row.area_id = await area_id(session, body.area_key)
    await session.flush()
    return await team_view(session, project)


async def open_teams(
    session: AsyncSession, event_id: UUID, target: int, grouped: dict[UUID, list[models.Enrollment]]
) -> list[OpenTeam]:
    result: list[OpenTeam] = []
    teams = await session.scalars(
        select(models.Project)
        .where(
            models.Project.event_id == event_id,
            models.Project.team_status == "formed",
            models.Project.seats_closed_at.is_(None),
        )
        .order_by(models.Project.created_at, models.Project.id)
    )
    for team in teams:
        size = len(await members(session, team.id))
        if size >= target:
            continue
        departed = set(
            await session.scalars(
                select(models.ProjectMember.user_id).where(
                    models.ProjectMember.project_id == team.id,
                    models.ProjectMember.left_at.is_not(None),
                )
            )
        )
        result.append(
            OpenTeam(
                id=team.id,
                size=size,
                banned_unit_ids=frozenset(
                    key
                    for key, group in grouped.items()
                    if any(row.user_id in departed for row in group)
                ),
            )
        )
    return result


async def refill_event(session: AsyncSession, event: models.Event, now: datetime) -> None:
    from hmm.formation.common import participation_event
    from hmm.formation.runs import apply_packing

    event, formation = await participation_event(session, event.id)
    if formation.phase != "formed" or event.status != "published" or now >= reg_deadline(event):
        return None
    target, minimum, maximum = formation_sizes(event, formation, await load_settings(session))
    units, grouped, parties = await queue_units(session, event.id)
    packing = pack(
        units, await open_teams(session, event.id, target, grouped), target, minimum, maximum
    )
    packing = packing.model_copy(
        update={
            "new_teams": [
                [party.id]
                for party in parties
                if party.closed and len(grouped.get(party.id, [])) >= minimum
            ]
            + packing.new_teams
        }
    )
    await apply_packing(session, event, packing, grouped, parties, now, refill=True)


async def finalize_event(session: AsyncSession, event: models.Event, now: datetime) -> None:
    from hmm.formation.common import formation_row

    formation = await formation_row(session, event.id)
    for project in await session.scalars(
        select(models.Project).where(
            models.Project.event_id == event.id, models.Project.team_status == "formed"
        )
    ):
        for row in await members(session, project.id):
            if row.state == "proposed":
                await remove_member(session, project, event, row, now, "lapsed", "lapsed")
        project.team_status = "active" if await members(session, project.id) else "disbanded"
        await audit(session, project.id, now, str(project.team_status))
    for row in await session.scalars(
        select(models.Enrollment).where(
            models.Enrollment.event_id == event.id,
            models.Enrollment.status.in_(["active", "reserve"]),
        )
    ):
        await set_enrollment_status(session, row, "expired", now)
    formation.phase = "closed"
    await session.flush()
