import asyncio
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


async def respond(h: Harness, actor: models.User, request_id: UUID, action: str) -> httpx.Response:
    async with await h.client(actor) as c:
        return await c.post(f"/api/v1/join-requests/{request_id}/{action}")


async def live_members(h: Harness, *conditions: Any) -> list[models.ProjectMember]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.left_at.is_(None), *conditions
                )
            )
        )


async def enrollment_row(h: Harness, enrollment_id: UUID) -> models.Enrollment:
    async with h.db() as session:
        return await session.get_one(models.Enrollment, enrollment_id)


async def project_row(h: Harness, project_id: UUID) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


async def always_open(*args: Any) -> bool:
    return True


async def test_accept_join_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, member, requester = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])
    await seed_team(h, event, await h.user(), name="Other")
    enrollment = await enroll(h, event, requester)
    request = await seed_request(h, team, requester)

    response = await respond(h, lead, request.id, "accept")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert datetime.fromisoformat(body["responded_at"]) == h.now
    assert body["allowed_actions"] == []
    [row] = await live_members(h, models.ProjectMember.user_id == requester.id)
    assert row.project_id == team.id
    assert row.role == "member"
    assert row.state == "confirmed"
    assert row.joined_via == "join_request"
    assert row.is_team_row is True
    assert row.area_id is None
    assert row.confirm_by is None
    stored = await enrollment_row(h, enrollment.id)
    assert stored.status == "placed"
    assert stored.status_at == h.now
    assert stored.reserve_rank is None
    assert (await project_row(h, team.id)).last_lead_action_at == h.now
    assert len(await notifications_for(h, requester, "join_request_accepted")) == 1
    assert len(await h.pending_jobs("refresh_recommendations")) == 2
    assert await h.pending_jobs("rank_reserve") == []


async def test_accept_reserve_requester_at_late_core(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    team = await seed_team(h, event, lead, origin="late_claim")
    await set_formation(h, event, phase="locked")
    requester = await h.user()
    enrollment = await enroll(h, event, requester, status="reserve")
    request = await seed_request(h, team, requester)

    assert_problem(await respond(h, lead, request.id, "accept"), 409, ErrorCode.NOT_ENROLLED)
    monkeypatch.setattr(service, "late_core_open", always_open)
    response = await respond(h, lead, request.id, "accept")
    assert response.status_code == 200, response.text
    assert (await enrollment_row(h, enrollment.id)).status == "placed"
    [job] = await h.pending_jobs("rank_reserve")
    assert job["args"]["event_id"] == str(event.id)


async def test_reject_join_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, requester = await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    enrollment = await enroll(h, event, requester)
    request = await seed_request(h, team, requester)

    response = await respond(h, lead, request.id, "reject")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert datetime.fromisoformat(body["responded_at"]) == h.now
    assert body["allowed_actions"] == []
    assert await live_members(h, models.ProjectMember.user_id == requester.id) == []
    stored = await enrollment_row(h, enrollment.id)
    assert (stored.status, stored.status_at) == ("active", enrollment.status_at)
    assert (await project_row(h, team.id)).last_lead_action_at == h.now
    assert len(await notifications_for(h, requester, "join_request_rejected")) == 1
    assert await h.pending_jobs() == []

    again = await respond(h, lead, request.id, "reject")
    assert again.status_code == 200, again.text
    assert len(await notifications_for(h, requester, "join_request_rejected")) == 1


async def test_respond_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead, member, requester = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead, members=[member])
    enrollment = await enroll(h, event, requester)
    request = await seed_request(h, team, requester)

    assert_problem(await respond(h, member, request.id, "accept"), 403, ErrorCode.FORBIDDEN)
    assert_problem(await respond(h, member, request.id, "reject"), 403, ErrorCode.FORBIDDEN)

    full_lead = await h.user()
    full = await seed_team(
        h, event, full_lead, members=[await h.user() for _ in range(4)], name="Full"
    )
    other_requester = await h.user()
    await enroll(h, event, other_requester)
    full_request = await seed_request(h, full, other_requester)
    assert_problem(await respond(h, full_lead, full_request.id, "accept"), 409, ErrorCode.TEAM_FULL)

    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(status="withdrawn")
        )
    assert_problem(await respond(h, lead, request.id, "accept"), 409, ErrorCode.NOT_ENROLLED)
    async with h.db() as session:
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(status="active")
        )

    taken = await h.user()
    await enroll(h, event, taken)
    taken_request = await seed_request(h, team, taken)
    await seed_team(h, event, await h.user(), members=[taken], name="Taken")
    assert_problem(await respond(h, lead, taken_request.id, "accept"), 409, ErrorCode.INVALID_STATE)

    await set_formation(h, event, phase="forming")
    assert_problem(
        await respond(h, lead, request.id, "accept"), 409, ErrorCode.FORMATION_IN_PROGRESS
    )
    assert_problem(
        await respond(h, lead, request.id, "reject"), 409, ErrorCode.FORMATION_IN_PROGRESS
    )
    await set_formation(h, event, phase="open")

    await set_formation(h, event, formation_at=h.now - timedelta(minutes=1))
    assert_problem(await respond(h, lead, request.id, "accept"), 409, ErrorCode.INVALID_STATE)
    await set_formation(h, event, formation_at=h.now + timedelta(days=1))

    async with h.db() as session:
        await session.execute(
            update(models.Project).where(models.Project.id == team.id).values(team_status="review")
        )
    assert_problem(await respond(h, lead, request.id, "accept"), 409, ErrorCode.INVALID_STATE)

    assert_problem(await respond(h, lead, uuid4(), "accept"), 404, ErrorCode.NOT_FOUND)
    assert_problem(await respond(h, lead, uuid4(), "reject"), 404, ErrorCode.NOT_FOUND)


async def test_respond_terminal(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, first, second = await h.user(), await h.user(), await h.user()
    team = await seed_team(h, event, lead)
    await enroll(h, event, first)
    await enroll(h, event, second)
    accepted = await seed_request(h, team, first)
    rejected = await seed_request(h, team, second)

    first_accept = await respond(h, lead, accepted.id, "accept")
    assert first_accept.status_code == 200, first_accept.text
    assert_problem(await respond(h, lead, accepted.id, "reject"), 409, ErrorCode.INVALID_STATE)
    h.advance(timedelta(minutes=5))
    again = await respond(h, lead, accepted.id, "accept")
    assert again.status_code == 200, again.text
    assert again.json()["responded_at"] == first_accept.json()["responded_at"]

    assert (await respond(h, lead, rejected.id, "reject")).status_code == 200
    assert_problem(await respond(h, lead, rejected.id, "accept"), 409, ErrorCode.INVALID_STATE)


async def test_respond_standalone(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead, member, requester, other = [await h.user() for _ in range(4)]
    project = await seed_standalone(h, lead, recruiting=RECRUITING, members=[member])
    request = await seed_request(h, project, requester)
    other_request = await seed_request(h, project, other)

    response = await respond(h, lead, request.id, "accept")
    assert response.status_code == 200, response.text
    [row] = await live_members(h, models.ProjectMember.user_id == requester.id)
    assert row.is_team_row is False
    assert row.event_id is None
    assert row.joined_via == "join_request"
    assert row.state == "confirmed"
    assert await count(h, models.Enrollment, models.Enrollment.user_id == requester.id) == 0
    assert await h.pending_jobs() == []
    assert (await project_row(h, project.id)).last_lead_action_at == h.now
    assert len(await notifications_for(h, requester, "join_request_accepted")) == 1

    response = await respond(h, lead, other_request.id, "reject")
    assert response.status_code == 200, response.text
    assert len(await notifications_for(h, other, "join_request_rejected")) == 1


async def test_accept_join_request_concurrency(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_max=5)
    lead = await h.user()
    team = await seed_team(h, event, lead, members=[await h.user() for _ in range(3)])
    x, y = await h.user(), await h.user()
    await enroll(h, event, x)
    await enroll(h, event, y)
    request_x = await seed_request(h, team, x)
    request_y = await seed_request(h, team, y)

    results = await asyncio.gather(
        respond(h, lead, request_x.id, "accept"), respond(h, lead, request_y.id, "accept")
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    [failed] = [response for response in results if response.status_code == 409]
    assert_problem(failed, 409, ErrorCode.TEAM_FULL)
    joined = await live_members(
        h,
        models.ProjectMember.project_id == team.id,
        models.ProjectMember.joined_via == "join_request",
    )
    assert len(joined) == 1
    assert len(await live_members(h, models.ProjectMember.project_id == team.id)) == 5


async def test_two_teams_fill_by_invite_and_join_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h, team_size_min=3, team_size_max=6, registration_deadline=h.now + timedelta(days=6)
    )
    lead_a, lead_b, invitee, requester = [await h.user() for _ in range(4)]
    team_a = await seed_team(h, event, lead_a, name="A")
    team_b = await seed_team(h, event, lead_b, name="B")
    invitee_enrollment = await enroll(h, event, invitee)
    requester_enrollment = await enroll(h, event, requester)

    async with await h.client(lead_a) as c:
        created = await c.post(
            f"/api/v1/projects/{team_a.id}/invites", json={"user_id": str(invitee.id)}
        )
    assert created.status_code == 201, created.text
    async with await h.client(invitee) as c:
        accepted = await c.post(f"/api/v1/invites/{created.json()['id']}/accept")
    assert accepted.status_code == 200, accepted.text

    async with await h.client(requester) as c:
        requested = await c.post(
            f"/api/v1/projects/{team_b.id}/join-requests", json={"note": "hello"}
        )
    assert requested.status_code == 201, requested.text
    response = await respond(h, lead_b, UUID(requested.json()["id"]), "accept")
    assert response.status_code == 200, response.text

    async with h.db() as session:
        for project_id, lead in ((team_a.id, lead_a), (team_b.id, lead_b)):
            project = await session.get_one(models.Project, project_id)
            view = await service.team_view(session, project, lead, h.now)
            assert len(view.members) == 2
            assert all(member.state == "confirmed" for member in view.members)
    assert (await enrollment_row(h, invitee_enrollment.id)).status == "placed"
    assert (await enrollment_row(h, requester_enrollment.id)).status == "placed"
