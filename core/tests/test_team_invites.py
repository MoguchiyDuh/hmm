import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                team_size_min=team_size_min,
                team_size_max=team_size_max,
                registration_deadline=registration_deadline,
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def seed_team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    *,
    team_status: str = "forming",
    members: Sequence[models.User] = (),
    origin: str = "claim",
    name: str = "Core",
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=lead.id,
            status="idea",
            team_status=team_status,
            origin=origin,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="claim" if is_lead else "invite",
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def enroll(
    h: Harness, event: models.Event, user: models.User, status: str = "active"
) -> models.Enrollment:
    async with h.db() as session:
        area_id = await session.scalar(select(models.Area.id).where(models.Area.key == "backend"))
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=False,
            status_at=h.now,
            preferred_area_id=area_id,
            note="hi",
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def seed_invite(
    h: Harness,
    project: models.Project,
    user: models.User,
    inviter: models.User,
    *,
    kind: str = "core",
    status: str = "pending",
    expires_in: timedelta = timedelta(days=7),
) -> models.Invite:
    async with h.db() as session:
        invite = models.Invite(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            invited_by=inviter.id,
            kind=kind,
            message=None,
            area_id=None,
            status=status,
            closed_reason=None,
            expires_at=h.now + expires_in,
            responded_at=None,
        )
        session.add(invite)
        await session.flush()
        await session.refresh(invite)
        return invite


async def invite(
    h: Harness, actor: models.User, project_id: UUID, user: models.User, **body: Any
) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(
            f"/api/v1/projects/{project_id}/invites", json={"user_id": str(user.id), **body}
        )


async def set_formation(h: Harness, event: models.Event, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(**values)
        )


async def set_invites_status(h: Harness, project: models.Project, status: str) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Invite)
            .where(models.Invite.project_id == project.id)
            .values(status=status)
        )


async def notifications_for(h: Harness, user: models.User, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user.id, models.Notification.type == kind
                )
            )
        )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def seed_standalone(
    h: Harness,
    lead: models.User,
    *,
    visibility: str = "public",
    status: str = "idea",
    members: Sequence[models.User] = (),
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=None,
            name="Side",
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=None,
            lead_user_id=lead.id,
            status=status,
            team_status=None,
            origin="standalone",
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=None,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="invite",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def test_core_invite_created(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, other, member = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[other])
    await enroll(h, event, member)
    response = await invite(h, lead, team.id, member, message="join us", area_key="backend")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "core"
    assert body["status"] == "pending"
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(days=7)
    assert body["message"] == "join us"
    assert body["area_key"] == "backend"
    assert body["project"]["id"] == str(team.id)
    assert body["user"]["id"] == str(member.id)
    assert body["invited_by"] == str(lead.id)
    assert body["inviter"]["display_name"] == lead.display_name
    assert body["responded_at"] is None
    assert body["allowed_actions"] == ["withdraw"]
    async with h.db() as session:
        rows = list(await session.scalars(select(models.Invite)))
        project = await session.get_one(models.Project, team.id)
    assert len(rows) == 1
    assert rows[0].event_id == event.id
    assert project.last_lead_action_at == h.now
    sent = await notifications_for(h, member, "invite_received")
    assert len(sent) == 1
    payload = cast(dict[str, Any], sent[0].payload)
    assert payload["kind"] == "core"
    assert payload["invite_id"] == str(rows[0].id)
    assert [action["key"] for action in payload["actions"]] == ["invite_accept", "invite_decline"]
    assert await h.pending_jobs() == []


async def test_invite_expiry_caps(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_formation(h, event, formation_at=h.now + timedelta(days=2))
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    response = await invite(h, lead, team.id, member)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=2)

    capped = await make_hackathon(h, registration_deadline=h.now + timedelta(days=3))
    fill_lead, fill_member = await h.user(), await h.user()
    fill = await seed_team(h, capped, fill_lead, team_status="review")
    response = await invite(h, fill_lead, fill.id, fill_member)
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "fill"
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=3)

    default = await make_hackathon(h)
    default_lead, default_member = await h.user(), await h.user()
    default_team = await seed_team(h, default, default_lead, team_status="review")
    response = await invite(h, default_lead, default_team.id, default_member)
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == h.now + timedelta(days=7)


async def test_core_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, a, b, c, d = [await h.user() for _ in range(5)]
    team = await seed_team(h, event, lead, members=[a])
    assert (await invite(h, lead, team.id, b)).status_code == 201
    assert_problem(await invite(h, lead, team.id, c), 409, ErrorCode.CORE_FULL)

    other_event = await make_hackathon(h, team_size_max=5)
    other_lead, x, y, z = [await h.user() for _ in range(4)]
    full = await seed_team(h, other_event, other_lead, members=[x, y])
    assert_problem(await invite(h, other_lead, full.id, z), 409, ErrorCode.CORE_FULL)

    await set_invites_status(h, team, "withdrawn")
    response = await invite(h, lead, team.id, d)
    assert response.status_code == 201, response.text


async def test_fill_limits(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, team_status="review", members=[member])
    response = await invite(h, lead, team.id, invitee)
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "fill"

    big_lead = await h.user()
    others = [await h.user() for _ in range(4)]
    big = await seed_team(h, event, big_lead, team_status="review", members=others, name="Big")
    assert_problem(await invite(h, big_lead, big.id, await h.user()), 409, ErrorCode.TEAM_FULL)

    late = await make_hackathon(h, registration_deadline=h.now - timedelta(minutes=1))
    late_lead = await h.user()
    locked = await seed_team(h, late, late_lead, team_status="locked")
    assert_problem(
        await invite(h, late_lead, locked.id, await h.user()), 409, ErrorCode.ENROLLMENT_CLOSED
    )


async def test_team_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, invitee = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])

    await set_formation(h, event, phase="forming")
    assert_problem(await invite(h, lead, team.id, invitee), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="locked")
    assert_problem(await invite(h, lead, team.id, invitee), 409, ErrorCode.INVALID_STATE)
    await set_formation(h, event, phase="open")

    active_lead = await h.user()
    active = await seed_team(h, event, active_lead, team_status="active", name="Active")
    assert_problem(await invite(h, active_lead, active.id, invitee), 409, ErrorCode.INVALID_STATE)

    assert_problem(await invite(h, lead, uuid4(), invitee), 404, ErrorCode.NOT_FOUND)
    assert_problem(await invite(h, member, team.id, invitee), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(invitee.id)}
        )
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    stranger = await h.user(onboarded=False)
    assert_problem(await invite(h, stranger, team.id, invitee), 403, ErrorCode.ONBOARDING_REQUIRED)
    assert await count(h, models.Invite) == 0


async def test_invitee_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)

    async with await h.client(lead) as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(uuid4())}
        )
    assert_problem(response, 404, ErrorCode.NOT_FOUND)
    assert_problem(
        await invite(h, lead, team.id, await h.user(onboarded=False)), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await invite(h, lead, team.id, await h.user(banned_at=h.now)), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await invite(h, lead, team.id, await h.user(dnd_indefinite=True)), 409, ErrorCode.USER_DND
    )
    taken = await h.user()
    await seed_team(h, event, await h.user(), members=[taken], name="Other")
    assert_problem(await invite(h, lead, team.id, taken), 409, ErrorCode.ALREADY_IN_TEAM)

    assert (await invite(h, lead, team.id, member)).status_code == 201
    assert_problem(await invite(h, lead, team.id, member), 409, ErrorCode.ALREADY_INVITED)
    assert await count(h, models.Invite, models.Invite.project_id == team.id) == 1
    assert len(await notifications_for(h, member, "invite_received")) == 1

    await set_invites_status(h, team, "withdrawn")
    response = await invite(h, lead, team.id, member)
    assert response.status_code == 201, response.text


async def test_standalone_invites(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, invitee, existing = await h.user(), await h.user(), await h.user()
    project = await seed_standalone(h, lead, members=[existing])
    response = await invite(h, lead, project.id, invitee)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "project"
    assert body["event_id"] is None
    assert datetime.fromisoformat(body["expires_at"]) == h.now + timedelta(days=7)
    assert len(await notifications_for(h, invitee, "project_invite_received")) == 1
    assert_problem(await invite(h, lead, project.id, existing), 409, ErrorCode.INVALID_STATE)

    hidden = await seed_standalone(h, await h.user(), visibility="members")
    random = await h.user()
    assert_problem(await invite(h, random, hidden.id, invitee), 404, ErrorCode.NOT_FOUND)

    abandoned_lead = await h.user()
    abandoned = await seed_standalone(h, abandoned_lead, status="abandoned")
    assert_problem(
        await invite(h, abandoned_lead, abandoned.id, invitee), 409, ErrorCode.INVALID_STATE
    )


async def test_invite_lists(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, first, second = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead, members=[member])
    older = await seed_invite(h, team, first, lead)
    newer = await seed_invite(h, team, second, lead)

    async with await h.client(lead) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/invites")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [str(newer.id), str(older.id)]
    assert all(row["allowed_actions"] == ["withdraw"] for row in response.json())

    async with await h.client(member) as c:
        assert_problem(await c.get(f"/api/v1/projects/{team.id}/invites"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(await h.admin()) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/invites")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2
    assert all(row["allowed_actions"] == [] for row in response.json())

    async with await h.client(first) as c:
        mine = await c.get("/api/v1/me/invites")
        pending = await c.get("/api/v1/me/invites", params={"status": "pending"})
        accepted = await c.get("/api/v1/me/invites", params={"status": "accepted"})
    assert mine.status_code == 200, mine.text
    assert [row["id"] for row in mine.json()] == [str(older.id)]
    assert mine.json()[0]["allowed_actions"] == ["accept", "decline"]
    assert pending.json() == mine.json()
    assert accepted.status_code == 200
    assert accepted.json() == []

    async with await h.client(member) as c:
        response = await c.get("/api/v1/me/invites")
    assert response.json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get("/api/v1/me/invites"), 401, ErrorCode.UNAUTHENTICATED)

    async with h.db() as session:
        await session.execute(
            update(models.Invite).where(models.Invite.id == older.id).values(invited_by=None)
        )
    async with await h.client(first) as c:
        response = await c.get("/api/v1/me/invites")
    assert response.status_code == 200, response.text
    assert response.json()[0]["inviter"] is None
    assert response.json()[0]["invited_by"] is None
