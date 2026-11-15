import itertools
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from harness import Harness
from hmm_contracts.algo import FormationPlan, PlannedTeam
from hmm_contracts.common import AreaImportance, EventKind, EventOrigin, EventStatus
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import tick
from sqlalchemy import select

_titles = itertools.count(1)

AREAS = (("backend", AreaImportance.PRIMARY), ("frontend", AreaImportance.NICE))


async def seed_areas(h: Harness) -> None:
    async with h.db() as session:
        if await session.scalar(select(models.Area.id).limit(1)) is None:
            await h.seed_areas()


async def make_event(
    h: Harness,
    *,
    minimum: int = 3,
    maximum: int = 5,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    status: EventStatus = EventStatus.PUBLISHED,
) -> models.Event:
    await seed_areas(h)
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=minimum,
                team_size_max=maximum,
                formation_enabled=True,
                areas=[
                    catalog.EventAreaInput(key=key, importance=importance)
                    for key, importance in AREAS
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=None,
            now=h.now,
        )


async def join(h: Harness, event: models.Event, user: models.User, **body: Any) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/events/{event.id}/enroll", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def formation(h: Harness, event: models.Event, user: models.User) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.get(f"/api/v1/events/{event.id}/formation")
    assert response.status_code == 200, response.text
    return response.json()


async def create_party(h: Harness, event: models.Event, user: models.User) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/events/{event.id}/party")
    assert response.status_code == 201, response.text
    return response.json()


async def invite_to_party(
    h: Harness, party_id: UUID, actor: models.User, user: models.User
) -> dict[str, Any]:
    async with await h.client(actor) as client:
        response = await client.post(
            f"/api/v1/parties/{party_id}/invites", json={"user_id": str(user.id)}
        )
    assert response.status_code == 201, response.text
    return response.json()


async def accept_invite(h: Harness, user: models.User, invite_id: UUID) -> None:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/invites/{invite_id}/accept")
    assert response.status_code == 200, response.text


async def seed_enrollment(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "active",
    status_at: datetime | None = None,
    party_id: UUID | None = None,
    willing_to_lead: bool = False,
) -> models.Enrollment:
    async with h.db() as session:
        row = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            status_at=status_at or h.now,
            party_id=party_id,
            willing_to_lead=willing_to_lead,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def seed_team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    members: tuple[models.User, ...] = (),
    *,
    state: str = "confirmed",
    accept_by: datetime | None = None,
    seats_closed: bool = False,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=f"Team {h.now:%H%M%S%f}",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="idea",
            team_status="formed",
            origin="formation",
            lead_user_id=lead.id,
            seats_closed_at=h.now if seats_closed else None,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if user.id == lead.id else "member",
                    state=state,
                    trust="self",
                    joined_via="formation",
                    accept_by=None
                    if state == "confirmed"
                    else accept_by or h.now + timedelta(days=1),
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now if state == "confirmed" else None,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def formation_row(h: Harness, event: models.Event) -> models.EventFormation:
    async with h.db() as session:
        return await session.get_one(models.EventFormation, event.id)


async def set_phase(h: Harness, event: models.Event, phase: str) -> None:
    async with h.db() as session:
        row = await session.get_one(models.EventFormation, event.id)
        row.phase = phase
        await session.flush()


async def set_dates(
    h: Harness,
    event: models.Event,
    *,
    starts_at: datetime | None = None,
    registration_deadline: datetime | None = None,
) -> None:
    async with h.db() as session:
        row = await session.get_one(models.Event, event.id)
        if starts_at is not None:
            row.starts_at = starts_at
        if registration_deadline is not None:
            row.registration_deadline = registration_deadline
        await session.flush()


async def maybe_enrollment(
    h: Harness, event: models.Event, user: models.User
) -> models.Enrollment | None:
    async with h.db() as session:
        return await session.scalar(
            select(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id)
            .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
            .limit(1)
        )


async def enrollment_row(h: Harness, event: models.Event, user: models.User) -> models.Enrollment:
    row = await maybe_enrollment(h, event, user)
    assert row is not None
    return row


async def queue_rows(h: Harness, event: models.Event) -> list[models.Enrollment]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Enrollment)
                .where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.status.in_(["active", "reserve"]),
                )
                .order_by(models.Enrollment.status_at, models.Enrollment.id)
            )
        )


async def parties(h: Harness, event: models.Event) -> list[models.Party]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Party)
                .where(models.Party.event_id == event.id)
                .order_by(models.Party.created_at, models.Party.id)
            )
        )


async def invites(h: Harness, event: models.Event) -> list[models.Invite]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Invite)
                .where(models.Invite.event_id == event.id)
                .order_by(models.Invite.created_at, models.Invite.id)
            )
        )


async def teams(h: Harness, event: models.Event) -> list[models.Project]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Project)
                .where(models.Project.event_id == event.id, models.Project.team_status.is_not(None))
                .order_by(models.Project.created_at, models.Project.id)
            )
        )


async def live_members(h: Harness, project: models.Project) -> list[models.ProjectMember]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )


async def stored_member(
    h: Harness, project: models.Project, user: models.User
) -> models.ProjectMember:
    async with h.db() as session:
        return (
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.user_id == user.id,
                )
            )
        ).one()


async def notices(
    h: Harness, *, user: models.User | None = None, kind: str | None = None
) -> list[models.Notification]:
    async with h.db() as session:
        query = select(models.Notification).order_by(
            models.Notification.created_at, models.Notification.id
        )
        if user is not None:
            query = query.where(models.Notification.user_id == user.id)
        if kind is not None:
            query = query.where(models.Notification.type == kind)
        return list(await session.scalars(query))


async def alerts(h: Harness, kind: str | None = None) -> list[models.AdminAlert]:
    async with h.db() as session:
        query = select(models.AdminAlert).order_by(models.AdminAlert.created_at)
        if kind is not None:
            query = query.where(models.AdminAlert.type == kind)
        return list(await session.scalars(query))


async def run_tick(h: Harness, event: models.Event) -> None:
    async with h.sessionmaker.begin() as session:
        await tick.tick_event(session, h.runtime, event.id, h.now)


async def form_now(h: Harness, event: models.Event, admin: models.User) -> dict[str, Any]:
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
    assert response.status_code == 202, response.text
    await h.run_jobs()
    return response.json()


async def formed(
    h: Harness,
    admin: models.User,
    *,
    count: int = 3,
    minimum: int = 3,
    maximum: int = 5,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
) -> tuple[models.Event, list[models.User], models.Project]:
    event = await make_event(
        h,
        minimum=minimum,
        maximum=maximum,
        starts_in=starts_in,
        registration_deadline=registration_deadline,
    )
    users = [await h.user() for _ in range(count)]
    for user in users:
        await join(h, event, user)
        h.advance(timedelta(seconds=1))
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    return event, users, team


async def confirm(h: Harness, user: models.User, team: models.Project) -> None:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/projects/{team.id}/placement/accept")
    assert response.status_code == 200, response.text


async def runs(h: Harness, event: models.Event, admin: models.User) -> list[dict[str, Any]]:
    async with await h.client(admin) as client:
        response = await client.get(f"/api/v1/admin/events/{event.id}/formation/runs")
    assert response.status_code == 200, response.text
    return response.json()


def payload(notification: models.Notification) -> dict[str, Any]:
    assert isinstance(notification.payload, dict)
    return notification.payload


def plan(*teams: list[UUID]) -> FormationPlan:
    return FormationPlan(
        teams=[PlannedTeam(units=list(units), reason="test plan") for units in teams],
        summary="test plan",
    )
