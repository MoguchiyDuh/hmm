import asyncio
import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    ConfirmKind,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
    JoinedVia,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.formation import placement_bounds
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.formation.schemas import CreateEnrollmentBody
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    kind: EventKind = EventKind.HACKATHON,
) -> models.Event:
    async with h.db() as session:
        payload = catalog.EventInput(
            kind=kind,
            title=f"Hack {next(_titles)}",
            starts_at=h.now + starts_in,
            ends_at=h.now + starts_in + timedelta(days=2),
            timezone="UTC",
            team_size_min=team_size_min,
            team_size_max=team_size_max,
            registration_deadline=registration_deadline,
        )
        return await catalog.create_event(
            session,
            payload,
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def set_status(h: Harness, event: models.Event, status: EventStatus) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Event).where(models.Event.id == event.id).values(status=status.value)
        )


async def set_phase(h: Harness, event: models.Event, phase: FormationPhase) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value)
        )


async def add_lead_fit(h: Harness, event: models.Event, user: models.User, fit: str) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(fit),
                reason="x",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE,
    **fields: Any,
) -> models.Enrollment:
    async with h.db() as session:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status.value,
            source=EnrollmentSource.SELF.value,
            status_at=h.now,
            **fields,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def make_team(
    h: Harness, event: models.Event, size: int, status: TeamStatus = TeamStatus.FORMING
) -> models.Project:
    members = [await h.user() for _ in range(size)]
    async with h.db() as session:
        team = models.Project(
            event_id=event.id,
            name=f"Team {next(_titles)}",
            description_md="Team description",
            goal="Build together",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.ACTIVE,
            team_status=status.value,
            origin=ProjectOrigin.ADMIN,
        )
        session.add(team)
        await session.flush()
        for index, member in enumerate(members):
            session.add(
                models.ProjectMember(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=member.id,
                    role=MemberRole.LEAD if index == 0 else MemberRole.MEMBER,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.ADMIN,
                    is_team_row=True,
                    joined_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(team)
        return team


async def get_formation(h: Harness, user: models.User, event_id: Any) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.get(f"/api/v1/events/{event_id}/formation")
    assert response.status_code == 200, response.text
    return response.json()


def projection_tuple(body: dict[str, Any]) -> tuple[int, int, int, int, int]:
    projection = body["projection"]
    return (
        projection["teams"],
        projection["new_teams"],
        projection["placed"],
        projection["reserve"],
        projection["undersized_cores"],
    )


async def test_default_hackathon_formation(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    body = await get_formation(h, member, event.id)
    assert body["event_id"] == str(event.id)
    assert body["phase"] == "open"
    assert (body["min_size"], body["max_size"], body["target"], body["viable"]) == (2, 5, 5, 4)
    assert body["core_max"] == 3
    assert datetime.fromisoformat(body["reg_deadline"]) == h.now + timedelta(days=30)
    assert datetime.fromisoformat(body["team_deadline"]) == h.now + timedelta(days=28)
    assert datetime.fromisoformat(body["formation_at"]) == h.now + timedelta(days=25)
    assert datetime.fromisoformat(body["review_ends_at"]) == h.now + timedelta(days=27)
    assert body["counts"] == {
        "enrollments": 0,
        "active": 0,
        "placed": 0,
        "reserve": 0,
        "cores": 0,
        "core_members": 0,
        "teams": 0,
        "volunteers": 0,
    }
    assert body["projection"] == {
        "teams": 0,
        "new_teams": 0,
        "placed": 0,
        "reserve": 0,
        "undersized_cores": 0,
        "team_sizes": [],
        "volunteers": 0,
    }
    assert body["my_state"] == {
        "enrollment": None,
        "team": None,
        "membership": None,
        "lead_fit": None,
    }
    assert body["allowed_actions"] == ["enroll"]


async def test_claim_lead_follows_lead_fit(harness: Harness) -> None:
    h = harness
    eligible = await h.user()
    low = await h.user()
    missing = await h.user()
    event = await make_hackathon(h)
    await add_lead_fit(h, event, eligible, "0.9")
    await add_lead_fit(h, event, low, "0.4")
    body = await get_formation(h, eligible, event.id)
    assert "claim_lead" in body["allowed_actions"]
    assert body["my_state"]["lead_fit"]["eligible"] is True
    assert body["my_state"]["lead_fit"]["fit"] == 0.9
    body = await get_formation(h, low, event.id)
    assert "claim_lead" not in body["allowed_actions"]
    assert body["my_state"]["lead_fit"]["eligible"] is False
    body = await get_formation(h, missing, event.id)
    assert "claim_lead" not in body["allowed_actions"]
    assert body["my_state"]["lead_fit"] is None


async def test_dnd_blocks_claim_lead(harness: Harness) -> None:
    h = harness
    busy = await h.user(dnd_indefinite=True)
    back = await h.user(dnd_until=h.now - timedelta(days=1))
    event = await make_hackathon(h)
    await add_lead_fit(h, event, busy, "0.9")
    await add_lead_fit(h, event, back, "0.9")
    body = await get_formation(h, busy, event.id)
    assert "enroll" in body["allowed_actions"]
    assert "claim_lead" not in body["allowed_actions"]
    body = await get_formation(h, back, event.id)
    assert "claim_lead" in body["allowed_actions"]


async def test_my_state_with_enrollment_and_proposed_membership(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend",))
    actor = await h.user()
    event = await make_hackathon(h)
    await add_lead_fit(h, event, actor, "0.9")
    enrollment = await enroll(
        h,
        event,
        actor,
        preferred_area_id=areas["backend"].id,
        note="Happy to help",
        willing_to_lead=True,
    )
    team = await make_team(h, event, 1)
    async with h.db() as session:
        session.add(
            models.ProjectMember(
                project_id=team.id,
                event_id=event.id,
                user_id=actor.id,
                role=MemberRole.MEMBER,
                state=MemberState.PROPOSED,
                trust=Trust.SELF,
                joined_via=JoinedVia.CLAIM,
                confirm_by=h.now + timedelta(days=1),
                confirm_kind=ConfirmKind.PLACEMENT,
                is_team_row=True,
                joined_at=h.now,
                area_id=None,
            )
        )
    body = await get_formation(h, actor, event.id)
    state = body["my_state"]
    assert state["enrollment"]["id"] == str(enrollment.id)
    assert state["enrollment"]["preferred_area_key"] == "backend"
    assert state["enrollment"]["note"] == "Happy to help"
    assert state["enrollment"]["willing_to_lead"] is True
    assert state["enrollment"]["status"] == "active"
    assert state["enrollment"]["team"]["id"] == str(team.id)
    assert state["enrollment"]["allowed_actions"] == ["edit"]
    assert state["team"]["id"] == str(team.id)
    assert state["team"]["team_status"] == "forming"
    assert state["membership"]["project_id"] == str(team.id)
    assert state["membership"]["user_id"] == str(actor.id)
    assert state["membership"]["state"] == "proposed"
    assert state["membership"]["area_key"] is None
    assert state["membership"]["allowed_actions"] == ["confirm_placement", "decline_placement"]
    assert "enroll" not in body["allowed_actions"]
    assert "claim_lead" not in body["allowed_actions"]
    assert body["allowed_actions"] == ["edit_enrollment"]


async def test_actions_by_phase(harness: Harness) -> None:
    h = harness
    member = await h.user()
    admin = await h.admin()
    event = await make_hackathon(h)
    assert (await get_formation(h, admin, event.id))["allowed_actions"] == [
        "enroll",
        "edit",
        "run",
    ]
    await set_phase(h, event, FormationPhase.FORMING)
    assert (await get_formation(h, member, event.id))["allowed_actions"] == []
    await set_phase(h, event, FormationPhase.REVIEW)
    assert (await get_formation(h, member, event.id))["allowed_actions"] == ["enroll"]
    assert (await get_formation(h, admin, event.id))["allowed_actions"] == ["enroll", "run"]
    await set_phase(h, event, FormationPhase.CLOSED)
    assert (await get_formation(h, member, event.id))["allowed_actions"] == []


async def test_open_projection_with_cores(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await make_team(h, event, 2)
    await make_team(h, event, 1)
    for _ in range(5):
        await enroll(h, event, await h.user())
    body = await get_formation(h, member, event.id)
    bounds = placement_bounds(5, [2, 1], 4, 5, 5)
    expected = (
        bounds.teams,
        bounds.new_teams,
        bounds.placed,
        bounds.reserve,
        bounds.undersized_cores,
    )
    assert projection_tuple(body) == expected == (2, 0, 5, 0, 0)
    assert body["projection"]["team_sizes"] == [4, 4]
    assert sum(body["projection"]["team_sizes"]) == 3 + body["projection"]["placed"]


async def test_open_projection_splits_new_teams(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h, team_size_max=6)
    for _ in range(11):
        await enroll(h, event, await h.user())
    body = await get_formation(h, member, event.id)
    assert (body["target"], body["viable"]) == (6, 5)
    assert projection_tuple(body) == (2, 2, 11, 0, 0)
    assert body["projection"]["team_sizes"] == [6, 5]


async def test_open_projection_cores_and_new_teams(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await make_team(h, event, 2)
    await make_team(h, event, 2)
    for _ in range(9):
        await enroll(h, event, await h.user())
    body = await get_formation(h, member, event.id)
    sizes = body["projection"]["team_sizes"]
    assert projection_tuple(body) == (3, 1, 9, 0, 0)
    assert len(sizes) == 3
    assert sum(sizes) == 4 + body["projection"]["placed"]
    assert all(1 <= size <= 5 for size in sizes)


@pytest.mark.parametrize("cores", [[], [1], [2, 2], [4]])
@pytest.mark.parametrize("free", range(13))
def test_projected_team_sizes_conserve_members(cores: list[int], free: int) -> None:
    bounds = placement_bounds(free, cores, 4, 5, 5)
    sizes = formation.projected_team_sizes(bounds.placed, cores, bounds.new_teams, 4, 5, 5)
    assert sum(sizes) == sum(cores) + bounds.placed
    assert len(sizes) == len(cores) + bounds.new_teams
    assert all(size <= 5 for size in sizes)


async def test_projection_frozen_after_open(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await make_team(h, event, 3, TeamStatus.REVIEW)
    await make_team(h, event, 2, TeamStatus.REVIEW)
    for _ in range(3):
        await enroll(h, event, await h.user(), EnrollmentStatus.PLACED)
    await enroll(h, event, await h.user(), EnrollmentStatus.RESERVE, reserve_rank=1)
    await set_phase(h, event, FormationPhase.REVIEW)
    body = await get_formation(h, member, event.id)
    assert body["projection"] == {
        "teams": 2,
        "new_teams": 0,
        "placed": 3,
        "reserve": 1,
        "undersized_cores": 0,
        "team_sizes": [3, 2],
        "volunteers": 0,
    }


async def test_formation_visibility(harness: Harness) -> None:
    h = harness
    member = await h.user()
    admin = await h.admin()
    draft = await make_hackathon(h)
    cancelled = await make_hackathon(h)
    await set_status(h, draft, EventStatus.DRAFT)
    await set_status(h, cancelled, EventStatus.CANCELLED)
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/events/{draft.id}/formation")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await client.get(f"/api/v1/events/{cancelled.id}/formation")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await client.get(f"/api/v1/events/{conference.id}/formation")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await client.get(f"/api/v1/events/{uuid4()}/formation")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    assert (await get_formation(h, admin, draft.id))["event_id"] == str(draft.id)
    await enroll(h, cancelled, member, EnrollmentStatus.WITHDRAWN)
    body = await get_formation(h, member, cancelled.id)
    assert body["my_state"]["enrollment"]["status"] == "withdrawn"
    assert body["allowed_actions"] == []
    async with h.anonymous() as client:
        response = await client.get(f"/api/v1/events/{draft.id}/formation")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)


async def enrollment_rows(h: Harness, event: models.Event) -> list[models.Enrollment]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Enrollment).where(models.Enrollment.event_id == event.id)
            )
        )


async def attendance_rows(h: Harness, event: models.Event) -> list[models.EventAttendance]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.EventAttendance).where(models.EventAttendance.event_id == event.id)
            )
        )


def enroll_url(event_id: Any) -> str:
    return f"/api/v1/events/{event_id}/enroll"


async def test_enroll_creates_active_enrollment(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend",))
    member = await h.user()
    event = await make_hackathon(h)
    body = {"preferred_area_key": "backend", "note": "hi", "willing_to_lead": True}
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "active"
    assert data["source"] == "self"
    assert data["preferred_area_key"] == "backend"
    assert data["note"] == "hi"
    assert data["willing_to_lead"] is True
    assert data["event_id"] == str(event.id)
    assert data["event"]["id"] == str(event.id)
    assert data["user"]["id"] == str(member.id)
    assert data["team"] is None
    assert data["reserve_rank"] is None
    assert data["allowed_actions"] == ["edit", "withdraw"]
    attendances = await attendance_rows(h, event)
    assert [(row.user_id, row.status, row.role, row.trust) for row in attendances] == [
        (member.id, "going", "attendee", "self")
    ]
    assert await h.pending_jobs("rank_reserve") == []
    assert await h.pending_jobs("refresh_recommendations") == []


async def test_enroll_refreshes_open_team_recommendations(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    forming = await make_team(h, event, 1)
    review = await make_team(h, event, 1, TeamStatus.REVIEW)
    await make_team(h, event, 1, TeamStatus.DISBANDED)
    await make_team(h, event, 1, TeamStatus.ACTIVE)
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={})
    assert response.status_code == 201, response.text
    jobs = await h.pending_jobs("refresh_recommendations")
    assert [job["args"]["project_id"] for job in jobs] == [str(forming.id), str(review.id)]


async def test_enroll_repeat_identical_and_conflicting(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await make_team(h, event, 1)
    body = {"note": "hi", "willing_to_lead": False}
    async with await h.client(member) as c:
        first = await c.post(enroll_url(event.id), json=body)
        assert first.status_code == 201, first.text
        jobs = await h.pending_jobs()
        second = await c.post(enroll_url(event.id), json=body)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]
        assert len(await enrollment_rows(h, event)) == 1
        assert len(await attendance_rows(h, event)) == 1
        assert await h.pending_jobs() == jobs
        conflict = await c.post(enroll_url(event.id), json={**body, "note": "other"})
        assert_problem(conflict, 409, ErrorCode.ALREADY_ENROLLED)
    rows = await enrollment_rows(h, event)
    assert [row.note for row in rows] == ["hi"]
    assert await h.pending_jobs() == jobs


async def test_enroll_preferred_area_validation(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    member = await h.user()
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={"preferred_area_key": "nope"})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert await enrollment_rows(h, event) == []
        response = await c.post(enroll_url(event.id), json={"preferred_area_key": "frontend"})
    assert response.status_code == 201, response.text
    assert response.json()["preferred_area_key"] == "frontend"


async def test_participation_blocked_while_forming(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await set_phase(h, event, FormationPhase.FORMING)
    async with await h.client(member) as c:
        assert_problem(
            await c.post(enroll_url(event.id), json={}), 409, ErrorCode.FORMATION_IN_PROGRESS
        )
        assert_problem(
            await c.patch(enroll_url(event.id), json={"note": "x"}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(await c.delete(enroll_url(event.id)), 409, ErrorCode.FORMATION_IN_PROGRESS)
    assert await enrollment_rows(h, event) == []


async def test_enroll_in_review_goes_to_reserve(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    team = await make_team(h, event, 1)
    await set_phase(h, event, FormationPhase.REVIEW)
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "reserve"
    assert response.json()["reserve_rank"] is None
    rank = await h.pending_jobs("rank_reserve")
    assert [job["args"] for job in rank] == [{"event_id": str(event.id)}]
    refresh = await h.pending_jobs("refresh_recommendations")
    assert [job["args"]["project_id"] for job in refresh] == [str(team.id)]


async def test_enroll_time_boundaries(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h, registration_deadline=h.now + timedelta(days=10))
    late = await make_hackathon(h, registration_deadline=h.now + timedelta(hours=1))
    await set_phase(h, late, FormationPhase.LOCKED)
    past = await make_hackathon(h, registration_deadline=h.now - timedelta(minutes=1))
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={})
        assert response.status_code == 201, response.text
        response = await c.delete(enroll_url(event.id))
        assert response.status_code == 204, response.text
        response = await c.post(enroll_url(late.id), json={})
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "reserve"
        assert [job["args"] for job in await h.pending_jobs("rank_reserve")] == [
            {"event_id": str(late.id)}
        ]
        assert_problem(await c.post(enroll_url(past.id), json={}), 409, ErrorCode.ENROLLMENT_CLOSED)
        h.advance(timedelta(days=10))
        assert_problem(
            await c.post(enroll_url(event.id), json={}), 409, ErrorCode.ENROLLMENT_CLOSED
        )


async def test_withdraw_active_enrollment(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={})
        assert response.status_code == 201, response.text
        h.advance(timedelta(hours=1))
        response = await c.delete(enroll_url(event.id))
        assert response.status_code == 204, response.text
        assert response.content == b""
        jobs = await h.pending_jobs()
        response = await c.delete(enroll_url(event.id))
        assert response.status_code == 204, response.text
    rows = await enrollment_rows(h, event)
    assert [(row.status, row.status_at, row.reserve_rank) for row in rows] == [
        ("withdrawn", h.now, None)
    ]
    attendances = await attendance_rows(h, event)
    assert [(row.status, row.status_at) for row in attendances] == [("cancelled", h.now)]
    assert await h.pending_jobs() == jobs


async def test_withdraw_reserve_and_attended(harness: Harness) -> None:
    h = harness
    reserve = await h.user()
    attended = await h.user()
    event = await make_hackathon(h)
    await enroll(h, event, reserve, EnrollmentStatus.RESERVE, reserve_rank=1)
    await enroll(h, event, attended)
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=attended.id,
                status=AttendanceStatus.ATTENDED,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )
    async with await h.client(reserve) as c:
        response = await c.delete(enroll_url(event.id))
    assert response.status_code == 204, response.text
    assert [job["args"] for job in await h.pending_jobs("rank_reserve")] == [
        {"event_id": str(event.id)}
    ]
    async with await h.client(attended) as c:
        response = await c.delete(enroll_url(event.id))
    assert response.status_code == 204, response.text
    assert [row.status for row in await attendance_rows(h, event)] == ["attended"]


async def test_team_member_cannot_enroll_or_withdraw(harness: Harness) -> None:
    h = harness
    event = await make_hackathon(h)
    team = await make_team(h, event, 1)
    async with h.db() as session:
        member_id = await session.scalar(
            select(models.ProjectMember.user_id).where(models.ProjectMember.project_id == team.id)
        )
        member = await session.get_one(models.User, member_id)
    async with await h.client(member) as c:
        assert_problem(await c.post(enroll_url(event.id), json={}), 409, ErrorCode.ALREADY_IN_TEAM)
        assert_problem(await c.delete(enroll_url(event.id)), 409, ErrorCode.ALREADY_IN_TEAM)


async def test_reactivation_reuses_enrollment(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend",))
    member = await h.user()
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        first = await c.post(enroll_url(event.id), json={"note": "first"})
        assert first.status_code == 201, first.text
        attendance_id = (await attendance_rows(h, event))[0].id
        assert (await c.delete(enroll_url(event.id))).status_code == 204
        second = await c.post(
            enroll_url(event.id),
            json={"note": "second", "preferred_area_key": "backend", "willing_to_lead": True},
        )
    assert second.status_code == 201, second.text
    data = second.json()
    assert data["id"] == first.json()["id"]
    assert data["status"] == "active"
    assert (data["note"], data["preferred_area_key"], data["willing_to_lead"]) == (
        "second",
        "backend",
        True,
    )
    assert len(await enrollment_rows(h, event)) == 1
    attendances = await attendance_rows(h, event)
    assert [(row.id, row.status) for row in attendances] == [(attendance_id, "going")]


async def test_edit_enrollment(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend",))
    member = await h.user()
    outsider = await h.user()
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        created = await c.post(
            enroll_url(event.id), json={"note": "old", "preferred_area_key": "backend"}
        )
        assert created.status_code == 201, created.text
        h.advance(timedelta(hours=1))
        response = await c.patch(enroll_url(event.id), json={"note": "new"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["note"] == "new"
        assert data["status"] == created.json()["status"]
        assert data["status_at"] == created.json()["status_at"]
        assert data["preferred_area_key"] == "backend"
        assert data["willing_to_lead"] is False
        response = await c.patch(enroll_url(event.id), json={"preferred_area_key": None})
        assert response.status_code == 200, response.text
        assert response.json()["preferred_area_key"] is None
        assert response.json()["note"] == "new"
        response = await c.patch(enroll_url(event.id), json={"willing_to_lead": True})
        assert response.status_code == 200, response.text
        assert response.json()["willing_to_lead"] is True
        jobs = await h.pending_jobs()
        response = await c.patch(enroll_url(event.id), json={"willing_to_lead": True})
        assert response.status_code == 200, response.text
        assert await h.pending_jobs() == jobs
        assert_problem(
            await c.patch(enroll_url(event.id), json={"preferred_area_key": "nope"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    async with await h.client(outsider) as c:
        assert_problem(
            await c.patch(enroll_url(event.id), json={"note": "x"}), 409, ErrorCode.NOT_ENROLLED
        )
    h.advance(timedelta(days=31))
    async with await h.client(member) as c:
        assert_problem(
            await c.patch(enroll_url(event.id), json={"note": "late"}),
            409,
            ErrorCode.ENROLLMENT_CLOSED,
        )


async def test_enroll_rollback_drops_jobs(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    await make_team(h, event, 1)
    await set_phase(h, event, FormationPhase.REVIEW)
    assert await h.pending_jobs() == []
    async with h.sessionmaker() as session:
        await formation.enroll(session, h.jobs, event.id, CreateEnrollmentBody(), member, h.now)
        await session.rollback()
    assert await enrollment_rows(h, event) == []
    assert await attendance_rows(h, event) == []
    assert await h.pending_jobs() == []


async def test_enrollment_visibility_and_auth(harness: Harness) -> None:
    h = harness
    member = await h.user()
    pending = await h.user(onboarded=False)
    admin = await h.admin()
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    draft = await make_hackathon(h)
    await set_status(h, draft, EventStatus.DRAFT)
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        for target in (conference.id, draft.id, uuid4()):
            assert_problem(await c.post(enroll_url(target), json={}), 404, ErrorCode.NOT_FOUND)
            assert_problem(
                await c.patch(enroll_url(target), json={"note": "x"}), 404, ErrorCode.NOT_FOUND
            )
            assert_problem(await c.delete(enroll_url(target)), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        assert_problem(await c.post(enroll_url(event.id), json={}), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(pending) as c:
        assert_problem(
            await c.post(enroll_url(event.id), json={}), 403, ErrorCode.ONBOARDING_REQUIRED
        )
    async with await h.client(admin) as c:
        assert_problem(
            await c.patch(enroll_url(event.id), json={"note": "x"}), 409, ErrorCode.NOT_ENROLLED
        )
        assert (await c.delete(enroll_url(event.id))).status_code == 204


async def test_dnd_member_can_enroll(harness: Harness) -> None:
    h = harness
    member = await h.user(dnd_indefinite=True)
    event = await make_hackathon(h)
    async with await h.client(member) as c:
        response = await c.post(enroll_url(event.id), json={})
    assert response.status_code == 201, response.text


async def test_concurrent_identical_enrolls(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_hackathon(h)
    async with await h.client(member) as first, await h.client(member) as second:
        responses = await asyncio.gather(
            first.post(enroll_url(event.id), json={"note": "same"}),
            second.post(enroll_url(event.id), json={"note": "same"}),
        )
    assert sorted(response.status_code for response in responses) == [200, 201]
    async with h.db() as session:
        live = await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status.in_(["active", "placed", "reserve"]),
            )
        )
    assert live == 1
    assert len(await attendance_rows(h, event)) == 1


def enrollments_url(event_id: Any) -> str:
    return f"/api/v1/events/{event_id}/enrollments"


async def join_team(
    h: Harness, team: models.Project, event: models.Event, user: models.User
) -> None:
    async with h.db() as session:
        session.add(
            models.ProjectMember(
                project_id=team.id,
                event_id=event.id,
                user_id=user.id,
                role=MemberRole.MEMBER,
                state=MemberState.CONFIRMED,
                trust=Trust.SELF,
                joined_via=JoinedVia.ADMIN,
                is_team_row=True,
                joined_at=h.now,
            )
        )


async def test_list_enrollments_roster(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend",))
    first = await h.user()
    second = await h.user()
    third = await h.user()
    viewer = await h.user()
    event = await make_hackathon(h)
    team = await make_team(h, event, 1)
    await enroll(h, event, first)
    await enroll(h, event, second, willing_to_lead=True, preferred_area_id=areas["backend"].id)
    await enroll(h, event, third, EnrollmentStatus.PLACED)
    await join_team(h, team, event, third)
    async with await h.client(viewer) as c:
        response = await c.get(enrollments_url(event.id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    items = body["items"]
    assert [item["user"]["id"] for item in items] == [str(first.id), str(second.id), str(third.id)]
    assert [item["user"]["display_name"] for item in items] == [
        first.display_name,
        second.display_name,
        third.display_name,
    ]
    assert [item["preferred_area_key"] for item in items] == [None, "backend", None]
    assert [item["willing_to_lead"] for item in items] == [False, True, False]
    assert [item["team"] for item in items[:2]] == [None, None]
    assert items[2]["team"]["id"] == str(team.id)
    assert items[2]["team"]["team_status"] == "forming"
    assert all(item["allowed_actions"] == [] for item in items)


async def test_list_enrollments_top_scores(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend", "mobile", "data", "design"))
    member = await h.user()
    event = await make_hackathon(h)
    await enroll(h, event, member)
    async with h.db() as session:
        await session.execute(
            update(models.Area)
            .where(models.Area.id == areas["design"].id)
            .values(archived_at=h.now)
        )
        for key, calculated, override in (
            ("backend", "8.0", None),
            ("frontend", "2.0", "6.5"),
            ("mobile", "3.0", None),
            ("data", "9.0", None),
            ("design", "10.0", None),
        ):
            session.add(
                models.AreaScore(
                    user_id=member.id,
                    area_id=areas[key].id,
                    calculated=Decimal(calculated),
                    calc_reason=f"calc {key}",
                    override=Decimal(override) if override else None,
                    override_reason="admin bump" if override else None,
                    override_at=h.now if override else None,
                )
            )
    async with await h.client(member) as c:
        response = await c.get(enrollments_url(event.id))
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["top_scores"] == [
        {"area_key": "data", "effective": 9.0, "reason": "calc data"},
        {"area_key": "backend", "effective": 8.0, "reason": "calc backend"},
        {"area_key": "frontend", "effective": 6.5, "reason": "admin bump"},
    ]
    assert item["allowed_actions"] == ["edit", "withdraw"]


async def test_list_enrollments_cursor(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    event = await make_hackathon(h)
    users = [await h.user() for _ in range(3)]
    for user in users:
        await enroll(h, event, user)
    seen: list[str] = []
    cursor: str | None = None
    async with await h.client(viewer) as c:
        for _ in range(3):
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = await c.get(enrollments_url(event.id), params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["user"]["id"])
            cursor = body["next_cursor"]
            if len(seen) < 3:
                assert cursor is not None
        assert cursor is None
        response = await c.get(enrollments_url(event.id), params={"cursor": "not-a-cursor"})
        assert_problem(response, 400, ErrorCode.BAD_REQUEST)
    assert seen == [str(user.id) for user in users]


async def test_list_enrollments_excludes_terminal(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    event = await make_hackathon(h)
    for status in (EnrollmentStatus.WITHDRAWN, EnrollmentStatus.LAPSED, EnrollmentStatus.EXPIRED):
        await enroll(h, event, await h.user(), status)
    reserve = await h.user()
    await enroll(h, event, reserve, EnrollmentStatus.RESERVE, reserve_rank=1)
    async with await h.client(viewer) as c:
        response = await c.get(enrollments_url(event.id))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(item["user"]["id"], item["status"], item["reserve_rank"]) for item in items] == [
        (str(reserve.id), "reserve", 1)
    ]


async def test_list_my_enrollments(harness: Harness) -> None:
    h = harness
    member = await h.user()
    reservist = await h.user()
    first = await make_hackathon(h)
    second = await make_hackathon(h)
    third = await make_hackathon(h)
    await enroll(h, second, member, EnrollmentStatus.WITHDRAWN)
    h.advance(timedelta(hours=1))
    active = await enroll(h, first, member)
    await enroll(h, third, reservist, EnrollmentStatus.RESERVE, reserve_rank=1)
    await enroll(h, third, reservist, EnrollmentStatus.WITHDRAWN)
    async with await h.client(member) as c:
        response = await c.get("/api/v1/me/enrollments")
    assert response.status_code == 200, response.text
    items = response.json()
    assert [(item["event_id"], item["status"]) for item in items] == [
        (str(first.id), "active"),
        (str(second.id), "withdrawn"),
    ]
    assert items[0]["id"] == str(active.id)
    assert all(item["user"]["id"] == str(member.id) for item in items)
    async with await h.client(reservist) as c:
        response = await c.get("/api/v1/me/enrollments", params={"status": "reserve"})
        assert response.status_code == 200, response.text
        assert [(item["event_id"], item["status"]) for item in response.json()] == [
            (str(third.id), "reserve")
        ]
        assert_problem(
            await c.get("/api/v1/me/enrollments", params={"status": "bogus"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )


async def test_list_enrollments_visibility_and_auth(harness: Harness) -> None:
    h = harness
    member = await h.user()
    pending = await h.user(onboarded=False)
    admin = await h.admin()
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    draft = await make_hackathon(h)
    await set_status(h, draft, EventStatus.DRAFT)
    async with await h.client(member) as c:
        for target in (conference.id, draft.id, uuid4()):
            assert_problem(await c.get(enrollments_url(target)), 404, ErrorCode.NOT_FOUND)
    async with await h.client(admin) as c:
        response = await c.get(enrollments_url(draft.id))
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}
    async with h.anonymous() as c:
        assert_problem(await c.get(enrollments_url(draft.id)), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await c.get("/api/v1/me/enrollments"), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(pending) as c:
        assert_problem(await c.get(enrollments_url(draft.id)), 403, ErrorCode.ONBOARDING_REQUIRED)
        assert_problem(await c.get("/api/v1/me/enrollments"), 403, ErrorCode.ONBOARDING_REQUIRED)


async def add_attendance(
    h: Harness,
    event: models.Event,
    user: models.User,
    status: AttendanceStatus = AttendanceStatus.GOING,
) -> None:
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=user.id,
                status=status,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )


async def user_rows(
    h: Harness, user: models.User
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int]:
    async with h.db() as session:
        enrollments = [
            (row.event_id, row.status, row.status_at, row.reserve_rank)
            for row in await session.scalars(
                select(models.Enrollment)
                .where(models.Enrollment.user_id == user.id)
                .order_by(models.Enrollment.event_id)
            )
        ]
        attendances = [
            (row.event_id, row.status, row.status_at)
            for row in await session.scalars(
                select(models.EventAttendance)
                .where(models.EventAttendance.user_id == user.id)
                .order_by(models.EventAttendance.event_id)
            )
        ]
        audits = await session.scalar(
            select(func.count())
            .select_from(models.AuditLog)
            .where(
                models.AuditLog.action == "user_ban",
                models.AuditLog.entity_id == str(user.id),
            )
        )
    return enrollments, attendances, audits or 0


async def test_ban_withdraws_live_enrollments(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    first = await make_hackathon(h)
    second = await make_hackathon(h)
    history = await make_hackathon(h)
    forming = await make_hackathon(h)
    await set_phase(h, forming, FormationPhase.FORMING)
    await enroll(h, first, member)
    await add_attendance(h, first, member)
    await enroll(h, second, member, EnrollmentStatus.RESERVE, reserve_rank=2)
    await add_attendance(h, second, member)
    await enroll(h, history, member, EnrollmentStatus.WITHDRAWN)
    await add_attendance(h, history, member, AttendanceStatus.ATTENDED)
    await enroll(h, forming, member)
    team = await make_team(h, first, 1)
    await join_team(h, team, first, member)
    history_at = h.now
    rank_jobs = await h.pending_jobs("rank_reserve")
    h.advance(timedelta(hours=1))
    async with await h.client(admin) as c:
        response = await c.post(f"/api/v1/admin/users/{member.id}/ban", json={"reason": "spam"})
    assert response.status_code == 200, response.text
    enrollments, attendances, audits = await user_rows(h, member)
    expected = {
        first.id: ("withdrawn", h.now, None),
        second.id: ("withdrawn", h.now, None),
        forming.id: ("withdrawn", h.now, None),
        history.id: ("withdrawn", history_at, None),
    }
    assert {row[0]: row[1:] for row in enrollments} == expected
    assert {row[0]: row[1:] for row in attendances} == {
        first.id: ("cancelled", h.now),
        second.id: ("cancelled", h.now),
        history.id: ("attended", history_at),
    }
    assert audits == 1
    async with h.db() as session:
        banned = await session.get_one(models.User, member.id)
        assert banned.banned_at == h.now
        assert banned.ban_reason == "spam"
        left_at = await session.scalar(
            select(models.ProjectMember.left_at).where(
                models.ProjectMember.project_id == team.id,
                models.ProjectMember.user_id == member.id,
            )
        )
    assert left_at == h.now
    assert await h.pending_jobs("rank_reserve") == rank_jobs
    before = await user_rows(h, member)
    h.advance(timedelta(hours=1))
    async with await h.client(admin) as c:
        response = await c.post(f"/api/v1/admin/users/{member.id}/ban", json={"reason": "again"})
    assert response.status_code == 200, response.text
    assert await user_rows(h, member) == before


async def test_withdraw_user_enrollments_direct(harness: Harness) -> None:
    h = harness
    member = await h.user()
    idle = await h.user()
    first = await make_hackathon(h)
    second = await make_hackathon(h)
    await enroll(h, first, member)
    await enroll(h, second, member, EnrollmentStatus.PLACED)
    async with h.db() as session:
        assert await formation.withdraw_user_enrollments(session, member.id, h.now) == sorted(
            [first.id, second.id]
        )
    before = await user_rows(h, member)
    h.advance(timedelta(hours=1))
    async with h.db() as session:
        assert await formation.withdraw_user_enrollments(session, member.id, h.now) == []
        assert await formation.withdraw_user_enrollments(session, idle.id, h.now) == []
    assert await user_rows(h, member) == before
