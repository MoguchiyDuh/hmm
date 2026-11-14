import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
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
from hmm_core.formation import service
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


async def seed_request(
    h: Harness,
    project: models.Project,
    user: models.User,
    *,
    status: str = "pending",
    note: str = "hi",
) -> models.JoinRequest:
    async with h.db() as session:
        request = models.JoinRequest(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            note=note,
            status=status,
            responded_at=None,
        )
        session.add(request)
        await session.flush()
        await session.refresh(request)
        return request


async def seed_standalone(
    h: Harness,
    lead: models.User,
    *,
    visibility: str = "public",
    status: str = "idea",
    recruiting: dict[str, Any] | None = None,
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
            recruiting=recruiting,
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


RECRUITING: dict[str, Any] = {"areas": ["backend"], "note": "x"}


async def request_join(
    h: Harness, actor: models.User, project_id: UUID, note: str = "we pour coffee"
) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(f"/api/v1/projects/{project_id}/join-requests", json={"note": note})


async def withdraw(h: Harness, actor: models.User, request_id: UUID) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.delete(f"/api/v1/join-requests/{request_id}")


async def set_formation(h: Harness, event: models.Event, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(**values)
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


async def request_row(h: Harness, request_id: UUID) -> models.JoinRequest:
    async with h.db() as session:
        return await session.get_one(models.JoinRequest, request_id)


async def test_create_join_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    await enroll(h, event, member)

    response = await request_join(h, member, team.id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["note"] == "we pour coffee"
    assert body["project"]["id"] == str(team.id)
    assert body["user"]["id"] == str(member.id)
    assert body["event_id"] == str(event.id)
    assert body["responded_at"] is None
    assert body["allowed_actions"] == ["withdraw"]
    assert await count(h, models.JoinRequest) == 1
    async with h.db() as session:
        project = await session.get_one(models.Project, team.id)
    assert project.last_lead_action_at == team.last_lead_action_at
    [notice] = await notifications_for(h, lead, "join_request_received")
    payload = cast(dict[str, Any], notice.payload)
    assert [action["key"] for action in payload["actions"]] == [
        "join_request_accept",
        "join_request_reject",
    ]
    assert await h.pending_jobs() == []


async def test_create_join_request_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    team = await seed_team(h, event, lead)

    assert_problem(await request_join(h, await h.user(), team.id), 409, ErrorCode.NOT_ENROLLED)

    locked_event = await make_hackathon(h)
    locked_team = await seed_team(h, locked_event, await h.user())
    reserve = await h.user()
    await enroll(h, locked_event, reserve, status="reserve")
    await set_formation(h, locked_event, phase="locked")
    assert_problem(await request_join(h, reserve, locked_team.id), 409, ErrorCode.NOT_ENROLLED)

    member = await h.user()
    await enroll(h, event, member)
    await set_formation(h, event, phase="forming")
    assert_problem(await request_join(h, member, team.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="open")

    review = await seed_team(h, event, await h.user(), team_status="review", name="Review")
    assert_problem(await request_join(h, member, review.id), 409, ErrorCode.INVALID_STATE)

    leaderless = await seed_team(h, event, await h.user(), name="Leaderless")
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == leaderless.id)
            .values(lead_user_id=None)
        )
    assert_problem(await request_join(h, member, leaderless.id), 409, ErrorCode.INVALID_STATE)

    taken = await h.user()
    await enroll(h, event, taken)
    await seed_team(h, event, await h.user(), members=[taken], name="Taken")
    assert_problem(await request_join(h, taken, team.id), 409, ErrorCode.ALREADY_IN_TEAM)

    other = await seed_team(h, event, await h.user(), name="Other")
    assert (await request_join(h, member, other.id)).status_code == 201
    assert_problem(await request_join(h, member, team.id), 409, ErrorCode.JOIN_REQUEST_PENDING)

    late = await h.user()
    await enroll(h, event, late)
    await set_formation(h, event, formation_at=h.now - timedelta(minutes=1))
    assert_problem(await request_join(h, late, team.id), 409, ErrorCode.INVALID_STATE)

    assert_problem(await request_join(h, member, uuid4()), 404, ErrorCode.NOT_FOUND)
    hidden = await seed_standalone(h, await h.user(), visibility="members", recruiting=RECRUITING)
    assert_problem(await request_join(h, member, hidden.id), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.post(f"/api/v1/projects/{team.id}/join-requests", json={"note": "hello"})
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)


async def test_create_join_request_with_dnd(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_team(h, event, await h.user())
    member = await h.user(dnd_indefinite=True)
    await enroll(h, event, member)
    response = await request_join(h, member, team.id)
    assert response.status_code == 201, response.text


async def test_late_core_closed(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_team(h, event, await h.user(), origin="late_claim")
    await set_formation(h, event, phase="locked")
    member = await h.user()
    await enroll(h, event, member, status="reserve")
    assert_problem(await request_join(h, member, team.id), 409, ErrorCode.NOT_ENROLLED)


async def test_late_core_open(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness

    async def always_open(*args: Any) -> bool:
        return True

    monkeypatch.setattr(service, "late_core_open", always_open)
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_team(h, event, await h.user(), origin="late_claim")
    await set_formation(h, event, phase="locked", formation_at=h.now - timedelta(hours=1))
    reserve, active = await h.user(), await h.user()
    await enroll(h, event, reserve, status="reserve")
    await enroll(h, event, active)

    response = await request_join(h, reserve, team.id)
    assert response.status_code == 201, response.text
    assert response.json()["event_id"] == str(event.id)
    response = await request_join(h, active, team.id)
    assert response.status_code == 201, response.text


async def test_standalone_join_requests(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, member, existing = await h.user(), await h.user(), await h.user()
    project = await seed_standalone(h, lead, recruiting=RECRUITING, members=[existing])

    response = await request_join(h, member, project.id, note="let me in")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["event_id"] is None
    assert body["note"] == "let me in"
    assert len(await notifications_for(h, lead, "project_join_request_received")) == 1
    assert_problem(await request_join(h, member, project.id), 409, ErrorCode.JOIN_REQUEST_PENDING)
    assert_problem(await request_join(h, existing, project.id), 409, ErrorCode.INVALID_STATE)

    quiet = await seed_standalone(h, await h.user())
    assert_problem(await request_join(h, member, quiet.id), 409, ErrorCode.INVALID_STATE)
    abandoned = await seed_standalone(h, await h.user(), status="abandoned", recruiting=RECRUITING)
    assert_problem(await request_join(h, member, abandoned.id), 409, ErrorCode.INVALID_STATE)


async def test_list_join_requests(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, first, second = [await h.user() for _ in range(4)]
    team = await seed_team(h, event, lead, members=[member])
    rejected = await seed_request(h, team, first, status="rejected")
    pending = await seed_request(h, team, second)

    async with await h.client(lead) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/join-requests")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["id"] for row in rows] == [str(pending.id), str(rejected.id)]
    assert rows[0]["allowed_actions"] == ["accept", "reject"]
    assert rows[1]["allowed_actions"] == []

    async with await h.client(member) as c:
        assert_problem(
            await c.get(f"/api/v1/projects/{team.id}/join-requests"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(await h.admin()) as c:
        response = await c.get(f"/api/v1/projects/{team.id}/join-requests")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2

    hidden = await seed_standalone(h, await h.user(), visibility="members")
    async with await h.client(member) as c:
        assert_problem(
            await c.get(f"/api/v1/projects/{hidden.id}/join-requests"), 404, ErrorCode.NOT_FOUND
        )


async def test_withdraw_join_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    enrollment = await enroll(h, event, member)
    request = await seed_request(h, team, member)
    async with h.db() as session:
        notices = await session.scalar(select(func.count()).select_from(models.Notification))

    response = await withdraw(h, member, request.id)
    assert response.status_code == 204, response.text
    assert response.content == b""
    row = await request_row(h, request.id)
    assert row.status == "withdrawn"
    assert row.responded_at == h.now
    async with h.db() as session:
        stored = await session.get_one(models.Enrollment, enrollment.id)
        assert (
            await session.scalar(select(func.count()).select_from(models.Notification)) == notices
        )
    assert stored.status == "active"
    assert await h.pending_jobs() == []


async def test_withdraw_join_request_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, other = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    request = await seed_request(h, team, member)

    assert_problem(await withdraw(h, lead, request.id), 403, ErrorCode.FORBIDDEN)
    assert_problem(await withdraw(h, other, request.id), 403, ErrorCode.FORBIDDEN)
    await set_formation(h, event, phase="forming")
    assert_problem(await withdraw(h, member, request.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_formation(h, event, phase="open")
    assert (await withdraw(h, member, request.id)).status_code == 204
    assert (await withdraw(h, member, request.id)).status_code == 204

    accepted = await seed_request(h, team, other, status="accepted")
    assert_problem(await withdraw(h, other, accepted.id), 409, ErrorCode.INVALID_STATE)
    assert_problem(await withdraw(h, member, uuid4()), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.delete(f"/api/v1/join-requests/{request.id}")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
