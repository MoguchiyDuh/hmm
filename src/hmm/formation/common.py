from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.pack import Unit
from hmm.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    ErrorCode,
    TeamStatus,
)
from hmm.formation import schemas
from hmm.identity.service import require_eligible
from hmm.platform.audit import record_audit
from hmm.platform.db import locked
from hmm.platform.errors import ProblemError
from hmm.platform.settings import RuntimeSettings, load_settings

LIVE_ENROLLMENT_STATES = frozenset(
    {EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE}
)
CAPACITY_TEAM_STATES = frozenset({TeamStatus.FORMED})


def formation_sizes(
    event: models.Event, formation: models.EventFormation, settings: RuntimeSettings
) -> tuple[int, int, int]:
    minimum = event.team_size_min or settings.team_size_default_min
    maximum = event.team_size_max or settings.team_size_default_max
    target = formation.target_override or maximum
    if not minimum <= target <= maximum:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Target must be within team size bounds")
    return target, minimum, maximum


def reg_deadline(event: models.Event) -> datetime:
    return event.registration_deadline or event.starts_at


def formation_time(
    event: models.Event,
    settings: RuntimeSettings,
    published_at: datetime,
    override: datetime | None = None,
) -> datetime:
    return max(
        override or reg_deadline(event) - timedelta(hours=settings.formation_lead_hours),
        published_at + timedelta(hours=1),
    )


def accept_by(event: models.Event, settings: RuntimeSettings, now: datetime) -> datetime:
    return min(now + timedelta(hours=settings.accept_hours), event.starts_at)


async def formation_event(
    session: AsyncSession, event_id: UUID, actor: models.User | None = None
) -> models.Event:
    event = await session.get(models.Event, event_id)
    if (
        event is None
        or not event.formation_enabled
        or (event.status in {"draft", "archived"} and (actor is None or not actor.is_admin))
    ):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event


async def locked_formation_event(
    session: AsyncSession, event_id: UUID, actor: models.User | None = None
) -> models.Event:
    event = await locked(session, models.Event, event_id)
    if event is None or not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event


async def formation_row(session: AsyncSession, event_id: UUID) -> models.EventFormation:
    row = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return row


async def participation_event(
    session: AsyncSession, event_id: UUID, actor: models.User | None = None
) -> tuple[models.Event, models.EventFormation]:
    event = await locked_formation_event(session, event_id, actor)
    formation = await formation_row(session, event_id)
    if formation.phase == "forming":
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS)
    return event, formation


def queue_open(event: models.Event, formation: models.EventFormation, now: datetime) -> None:
    if (
        event.status != "published"
        or formation.phase not in {"open", "formed"}
        or now >= reg_deadline(event)
    ):
        raise ProblemError(ErrorCode.ENROLLMENT_CLOSED)


async def ensure_formation(
    session: AsyncSession, event: models.Event, now: datetime
) -> models.EventFormation | None:
    if not event.formation_enabled:
        return None
    await locked_formation_event(session, event.id)
    row = await session.get(models.EventFormation, event.id)
    if row is None:
        row = models.EventFormation(
            event_id=event.id,
            phase="open",
            formation_at=formation_time(event, await load_settings(session), now),
            created_at=now,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
    return row


async def dates_changed(session: AsyncSession, event: models.Event, now: datetime) -> None:
    if not event.formation_enabled:
        return
    await locked_formation_event(session, event.id)
    row = await formation_row(session, event.id)
    if row.phase == "open":
        row.formation_at = formation_time(
            event, await load_settings(session), row.created_at, row.formation_at_override
        )
        await session.flush()


async def cancel_formation(session: AsyncSession, event: models.Event, now: datetime) -> None:
    if not event.formation_enabled:
        return
    await locked_formation_event(session, event.id)
    for team in await session.scalars(
        select(models.Project).where(
            models.Project.event_id == event.id,
            models.Project.team_status.in_(["formed", "active"]),
        )
    ):
        team.team_status = "cancelled"
        await audit(session, team.id, now, "cancelled")
    await session.flush()


async def live_enrollment_row(
    session: AsyncSession, event_id: UUID, user_id: UUID
) -> models.Enrollment | None:
    return await session.scalar(
        select(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.user_id == user_id,
            models.Enrollment.status.in_(LIVE_ENROLLMENT_STATES),
        )
        .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
        .limit(1)
    )


async def team_member(
    session: AsyncSession, event_id: UUID, user_id: UUID
) -> models.ProjectMember | None:
    return await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.event_id == event_id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.is_team_row,
            models.ProjectMember.left_at.is_(None),
        )
    )


async def members(session: AsyncSession, project_id: UUID) -> list[models.ProjectMember]:
    return list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def membership_row(
    session: AsyncSession, project_id: UUID, user_id: UUID
) -> models.ProjectMember | None:
    return await session.scalar(
        select(models.ProjectMember)
        .where(
            models.ProjectMember.project_id == project_id, models.ProjectMember.user_id == user_id
        )
        .order_by(models.ProjectMember.joined_at.desc(), models.ProjectMember.id.desc())
        .limit(1)
    )


async def locked_team(
    session: AsyncSession, project_id: UUID, actor: models.User | None = None
) -> tuple[models.Project, models.Event, models.EventFormation]:
    project = await session.get(models.Project, project_id)
    if project is None or project.event_id is None or project.team_status is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event, formation = await participation_event(session, project.event_id, actor)
    await session.refresh(project)
    return project, event, formation


def require_lead(project: models.Project, actor: models.User) -> None:
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)


async def eligible_user(
    session: AsyncSession, user_id: UUID, event_id: UUID, now: datetime, *, invite: bool = False
) -> models.User:
    user = await require_eligible(session, user_id, now, invite=invite)
    if await team_member(session, event_id, user_id) is not None:
        raise ProblemError(ErrorCode.ALREADY_IN_TEAM)
    return user


async def area_id(session: AsyncSession, key: str | None) -> UUID | None:
    if key is None:
        return None
    area = await session.scalar(
        select(models.Area).where(models.Area.key == key, models.Area.archived_at.is_(None))
    )
    if area is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Unknown area")
    return area.id


async def queue_units(
    session: AsyncSession, event_id: UUID
) -> tuple[list[Unit], dict[UUID, list[models.Enrollment]], list[models.Party]]:
    rows = list(
        await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status.in_(["active", "reserve"]),
            )
            .order_by(models.Enrollment.status_at, models.Enrollment.id)
        )
    )
    parties = list(
        await session.scalars(
            select(models.Party)
            .where(models.Party.event_id == event_id)
            .order_by(models.Party.created_at, models.Party.id)
        )
    )
    grouped: dict[UUID, list[models.Enrollment]] = {}
    for row in rows:
        grouped.setdefault(row.party_id or row.id, []).append(row)
    party_map = {party.id: party for party in parties}
    units = [
        Unit(
            id=key,
            size=len(group),
            queued_at=party_map[key].created_at if key in party_map else group[0].status_at,
        )
        for key, group in grouped.items()
        if key not in party_map or not party_map[key].closed
    ]
    return sorted(units, key=lambda unit: (unit.queued_at, unit.id)), grouped, parties


async def pick_lead(
    session: AsyncSession,
    rows: list[models.Enrollment],
    parties: list[models.Party],
    confirmed: set[UUID] | None = None,
    project_id: UUID | None = None,
) -> UUID | None:
    if not rows:
        return None
    candidates = [row for row in rows if confirmed is None or row.user_id in confirmed] or rows
    ids = {row.user_id for row in candidates}
    owners = [
        (
            -sum(row.party_id == party.id for row in rows),
            party.created_at,
            party.id,
            party.owner_user_id,
        )
        for party in parties
        if party.owner_user_id in ids
    ]
    if project_id is not None:
        entries = await session.scalars(
            select(models.AuditLog).where(
                models.AuditLog.entity_id == str(project_id), models.AuditLog.action == "member_add"
            )
        )
        for entry in entries:
            provenance = entry.after.get("parties") if isinstance(entry.after, dict) else None
            if isinstance(provenance, list):
                for party in provenance:
                    if isinstance(party, dict):
                        owner = UUID(str(party["owner"]))
                        if owner in ids:
                            owners.append(
                                (
                                    -int(str(party["size"])),
                                    datetime.fromisoformat(str(party["created_at"])),
                                    UUID(str(party["id"])),
                                    owner,
                                )
                            )
    if owners:
        return min(owners)[3]
    return min(candidates, key=lambda row: (not row.willing_to_lead, row.status_at, row.id)).user_id


async def set_enrollment_status(
    session: AsyncSession, row: models.Enrollment, status: str, now: datetime
) -> None:
    row.status = status
    if status != "placed":
        row.status_at = now
    if status in {"withdrawn", "lapsed", "expired"}:
        attendance = await session.scalar(
            select(models.EventAttendance).where(
                models.EventAttendance.event_id == row.event_id,
                models.EventAttendance.user_id == row.user_id,
                models.EventAttendance.status == "going",
            )
        )
        if attendance is not None:
            attendance.status = "cancelled"
            attendance.status_at = now
    await session.flush()


def enrollment_view(row: models.Enrollment) -> schemas.Enrollment:
    return schemas.Enrollment.model_validate(row, from_attributes=True)


async def team_view(session: AsyncSession, project: models.Project) -> schemas.Team:
    await session.flush()
    await session.refresh(project)
    rows = await members(session, project.id)
    for row in rows:
        await session.refresh(row)
    return schemas.Team(
        id=project.id,
        name=project.name,
        lead_user_id=project.lead_user_id,
        team_status=project.team_status,
        closed=project.seats_closed_at is not None,
        members=[schemas.Member.model_validate(row, from_attributes=True) for row in rows],
    )


async def audit(
    session: AsyncSession,
    entity_id: UUID,
    now: datetime,
    state: str,
    actor: models.User | None = None,
    action: AuditAction = AuditAction.SYSTEM_TRANSITION,
    reason: str | None = None,
) -> None:
    await record_audit(
        session,
        actor=actor.id if actor else None,
        actor_kind=ActorKind.ADMIN
        if actor and actor.is_admin
        else ActorKind.USER
        if actor
        else ActorKind.SYSTEM,
        action=action,
        entity_type=AuditEntityType.PROJECT,
        entity_id=entity_id,
        after={"state": state, "at": now.isoformat()},
        reason=reason,
    )
