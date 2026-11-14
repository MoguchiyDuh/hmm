import asyncio
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    ErrorCode,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberState,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_core import models
from hmm_core.formation import service as formation
from hmm_core.projects import service as projects
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem
from test_placement_confirmation import add_member, enroll, make_hackathon, seed_project


async def swap_teams(
    h: Harness, event: models.Event
) -> tuple[
    models.Project, models.Project, models.User, models.User, models.User, models.User, models.User
]:
    team_a = await seed_project(h, event, name="Team A", review_ends_at=h.now + timedelta(days=1))
    team_b = await seed_project(h, event, name="Team B", review_ends_at=h.now + timedelta(days=1))
    lead_a, lead_b, a1, a2, b1 = [await h.user() for _ in range(5)]
    for project, user, lead in (
        (team_a, lead_a, True),
        (team_b, lead_b, True),
        (team_a, a1, False),
        (team_a, a2, False),
        (team_b, b1, False),
    ):
        await enroll(h, event, user, status="placed")
        await add_member(h, event, project, user, role="lead" if lead else "member", set_lead=lead)
    return team_a, team_b, lead_a, lead_b, a1, a2, b1


async def two_teams(
    h: Harness,
    *,
    team_a_ends: datetime | None = None,
    team_b_ends: datetime | None = None,
) -> tuple[
    models.Event,
    models.Project,
    models.Project,
    models.User,
    models.User,
    models.User,
    models.User,
    models.User,
]:
    await h.seed_areas()
    event = await make_hackathon(h)
    team_a, team_b, lead_a, lead_b, a1, a2, b1 = await swap_teams(h, event)
    async with h.db() as session:
        for team, deadline in ((team_a, team_a_ends), (team_b, team_b_ends)):
            await session.execute(
                update(models.Project)
                .where(models.Project.id == team.id)
                .values(review_ends_at=deadline or h.now + timedelta(days=1))
            )
    return event, team_a, team_b, lead_a, lead_b, a1, a2, b1


async def propose(
    h: Harness,
    team: models.Project,
    lead: models.User,
    from_user: models.User,
    to_user: models.User,
) -> dict[str, Any]:
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{team.id}/swaps",
            json={
                "from_user_id": str(from_user.id),
                "to_user_id": str(to_user.id),
            },
        )
    assert response.status_code == 201, response.text
    return response.json()


async def notices(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


def payload(row: models.Notification) -> dict[str, Any]:
    return cast(dict[str, Any], row.payload)


async def test_propose_swap(harness: Harness) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, a2, b1 = await two_teams(h)
    body = await propose(h, team_a, lead_a, a1, b1)
    assert body["status"] == "pending"
    assert body["from_team_id"] == str(team_a.id)
    assert body["to_team_id"] == str(team_b.id)
    assert body["requested_by"] == str(lead_a.id)
    assert all(
        body[key] is None
        for key in ("to_lead_accepted_at", "from_user_accepted_at", "to_user_accepted_at")
    )
    assert body["allowed_actions"] == ["accept", "decline"]
    rows = await notices(h, "swap_request_received")
    assert {row.user_id for row in rows} == {lead_b.id, a1.id, b1.id}
    for row in rows:
        assert payload(row)["swap_request_id"] == body["id"]
        assert datetime.fromisoformat(payload(row)["expires_at"]) == h.now + timedelta(days=1)
        assert payload(row)["project_id"] == str(team_a.id if row.user_id == a1.id else team_b.id)
        assert [action["key"] for action in payload(row)["actions"]] == [
            "swap_accept",
            "swap_decline",
        ]
    assert a2.id not in {row.user_id for row in rows}
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.SwapRequest)) == 1
        assert (await session.get_one(models.Project, team_a.id)).last_lead_action_at == h.now
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        assert (await session.get_one(models.Event, event.id)).formation_enabled


@pytest.mark.parametrize("concurrent", [False, True])
async def test_accept_swap(harness: Harness, concurrent: bool) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == a1.id)
            .values(
                state=MemberState.PROPOSED.value,
                confirm_by=h.now + timedelta(hours=5),
                confirm_kind="placement",
            )
        )
        old = list(
            await session.scalars(
                select(models.ProjectMember).where(models.ProjectMember.user_id.in_([a1.id, b1.id]))
            )
        )
    body = await propose(h, team_a, lead_a, a1, b1)

    async def accept(user: models.User) -> dict[str, Any]:
        async with await h.client(user) as client:
            response = await client.post(f"/api/v1/swaps/{body['id']}/accept")
        assert response.status_code == 200, response.text
        return response.json()

    if concurrent:
        responses = await asyncio.gather(*(accept(user) for user in (lead_b, a1, b1)))
        assert sum(response["status"] == "accepted" for response in responses) == 1
        final = next(response for response in responses if response["status"] == "accepted")
    else:
        assert (await accept(lead_b))["status"] == "pending"
        assert (await accept(a1))["status"] == "pending"
        final = await accept(b1)
    assert final["status"] == "accepted"
    assert final["allowed_actions"] == []
    assert await accept(b1) == final
    rows = await notices(h, "swap_result")
    assert len(rows) == 4
    assert all(payload(row)["status"] == "accepted" for row in rows)
    jobs = [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]
    assert len(jobs) == 2
    assert {job["args"]["project_id"] for job in jobs} == {str(team_a.id), str(team_b.id)}
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ProjectMember)) == 7
        for before in old:
            closed = await session.get_one(models.ProjectMember, before.id)
            assert closed.left_at == h.now
            assert closed.left_reason == LeftReason.SWAPPED.value
            live = list(
                await session.scalars(
                    select(models.ProjectMember).where(
                        models.ProjectMember.user_id == before.user_id,
                        models.ProjectMember.left_at.is_(None),
                    )
                )
            )
            assert len(live) == 1
            after = live[0]
            assert after.project_id == (team_b.id if before.user_id == a1.id else team_a.id)
            assert after.joined_via == JoinedVia.SWAP.value
            for field in (
                "state",
                "confirm_by",
                "confirm_kind",
                "confirmed_at",
                "area_id",
                "trust",
                "proposal_reason",
            ):
                assert getattr(after, field) == getattr(before, field)
        for team, lead, size in ((team_a, lead_a, 3), (team_b, lead_b, 2)):
            assert (await session.get_one(models.Project, team.id)).lead_user_id == lead.id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(models.ProjectMember)
                    .where(
                        models.ProjectMember.project_id == team.id,
                        models.ProjectMember.left_at.is_(None),
                    )
                )
                == size
            )
        assert set(
            await session.scalars(
                select(models.Enrollment.status).where(models.Enrollment.event_id == event.id)
            )
        ) == {"placed"}
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    async with await h.client(lead_b) as client:
        assert_problem(
            await client.post(f"/api/v1/swaps/{body['id']}/decline"), 409, ErrorCode.INVALID_STATE
        )


async def test_decline_swap(harness: Harness) -> None:
    h = harness
    _, team_a, _, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    body = await propose(h, team_a, lead_a, a1, b1)
    async with await h.client(lead_b) as client:
        response = await client.post(f"/api/v1/swaps/{body['id']}/decline")
        assert response.status_code == 200
        assert response.json()["status"] == "declined"
        repeated = await client.post(f"/api/v1/swaps/{body['id']}/decline")
        assert repeated.json() == response.json()
    async with await h.client(a1) as client:
        assert_problem(
            await client.post(f"/api/v1/swaps/{body['id']}/accept"), 409, ErrorCode.INVALID_STATE
        )
    rows = await notices(h, "swap_result")
    assert len(rows) == 4
    assert all(payload(row)["status"] == "declined" for row in rows)
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(models.ProjectMember.left_at.is_not(None))
            )
            == 0
        )
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_swap_deadline(harness: Harness) -> None:
    h = harness
    _, team_a, _, lead_a, _, a1, _, b1 = await two_teams(
        h,
        team_a_ends=h.now + timedelta(hours=1),
        team_b_ends=h.now + timedelta(hours=5),
    )
    body = await propose(h, team_a, lead_a, a1, b1)
    assert all(
        datetime.fromisoformat(payload(row)["expires_at"]) == h.now + timedelta(hours=1)
        for row in await notices(h, "swap_request_received")
    )
    h.advance(timedelta(hours=1))
    async with await h.client(b1) as client:
        assert_problem(
            await client.post(f"/api/v1/swaps/{body['id']}/accept"), 409, ErrorCode.INVALID_STATE
        )
    async with h.db() as session:
        row = await session.get_one(models.SwapRequest, UUID(body["id"]))
        assert row.status == SwapRequestStatus.EXPIRED.value
        assert (await formation.swap_view(session, row, b1, h.now)).allowed_actions == []
    rows = await notices(h, "swap_result")
    assert len(rows) == 4
    assert all(payload(row)["status"] == "expired" for row in rows)


@pytest.mark.parametrize("change", ["member", "team", "lead"])
async def test_swap_changed_participants(harness: Harness, change: str) -> None:
    h = harness
    _, team_a, _, lead_a, lead_b, a1, _, b1 = await two_teams(h)
    body = await propose(h, team_a, lead_a, a1, b1)
    for user in (a1, b1):
        async with await h.client(user) as client:
            assert (await client.post(f"/api/v1/swaps/{body['id']}/accept")).status_code == 200
    async with h.db() as session:
        if change == "member":
            await session.execute(
                update(models.ProjectMember)
                .where(models.ProjectMember.user_id == a1.id)
                .values(left_at=h.now, left_reason="left")
            )
        else:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == team_a.id)
                .values(
                    **({"team_status": "locked"} if change == "team" else {"lead_user_id": a1.id})
                )
            )
    async with await h.client(lead_b) as client:
        response = await client.post(f"/api/v1/swaps/{body['id']}/accept")
    assert_problem(
        response, 409, ErrorCode.LEAD_NOT_SWAPPABLE if change == "lead" else ErrorCode.INVALID_STATE
    )
    async with h.db() as session:
        assert (await session.get_one(models.SwapRequest, UUID(body["id"]))).status == (
            "pending" if change == "lead" else "closed"
        )
        assert await session.scalar(select(func.count()).select_from(models.ProjectMember)) == 5
    rows = await notices(h, "swap_result")
    assert len(rows) == (0 if change == "lead" else 4)
    assert all(payload(row)["status"] == "closed" for row in rows)


@pytest.mark.parametrize(
    "gate",
    [
        "nonlead",
        "unknown",
        "standalone",
        "from_missing",
        "to_missing",
        "same_team",
        "from_lead",
        "to_lead",
        "from_locked",
        "to_locked",
        "forming",
        "cancelled",
        "anonymous",
    ],
)
async def test_swap_propose_gates(harness: Harness, gate: str) -> None:
    h = harness
    event, team_a, team_b, lead_a, lead_b, a1, a2, b1 = await two_teams(h)
    project_id = team_a.id
    from_id, to_id = a1.id, b1.id
    expected = ErrorCode.INVALID_STATE
    status = 409
    if gate in {"unknown", "standalone", "from_missing", "to_missing"}:
        expected, status = ErrorCode.NOT_FOUND, 404
    if gate == "unknown":
        project_id = uuid4()
    if gate == "standalone":
        async with h.db() as session:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == team_a.id)
                .values(event_id=None, team_status=None)
            )
    if gate == "from_missing":
        from_id = uuid4()
    if gate == "to_missing":
        to_id = uuid4()
    if gate == "same_team":
        to_id = a2.id
    if gate in {"from_lead", "to_lead"}:
        expected = ErrorCode.LEAD_NOT_SWAPPABLE
        if gate == "from_lead":
            from_id = lead_a.id
        else:
            to_id = lead_b.id
    async with h.db() as session:
        if gate in {"from_locked", "to_locked"}:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == (team_a.id if gate == "from_locked" else team_b.id))
                .values(team_status="locked")
            )
        if gate == "forming":
            await session.execute(
                update(models.EventFormation)
                .where(models.EventFormation.event_id == event.id)
                .values(phase=FormationPhase.FORMING.value)
            )
            expected = ErrorCode.FORMATION_IN_PROGRESS
        if gate == "cancelled":
            await session.execute(
                update(models.Event)
                .where(models.Event.id == event.id)
                .values(status=EventStatus.CANCELLED.value)
            )
    if gate == "anonymous":
        async with h.anonymous() as client:
            response = await client.post(
                f"/api/v1/projects/{project_id}/swaps",
                json={"from_user_id": str(from_id), "to_user_id": str(to_id)},
            )
        assert response.status_code == 401
        return
    if gate == "nonlead":
        expected, status = ErrorCode.FORBIDDEN, 403
    async with await h.client(a1 if gate == "nonlead" else lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{project_id}/swaps",
            json={"from_user_id": str(from_id), "to_user_id": str(to_id)},
        )
    assert_problem(response, status, expected)


@pytest.mark.parametrize("kind", ["swap", "merge"])
async def test_swap_pending_guard(harness: Harness, kind: str) -> None:
    h = harness
    event, team_a, team_b, lead_a, _, a1, _, b1 = await two_teams(h)
    if kind == "swap":
        await propose(h, team_a, lead_a, a1, b1)
    else:
        async with h.db() as session:
            session.add(
                models.MergeProposal(
                    event_id=event.id,
                    absorbed_team_id=team_a.id,
                    target_team_id=team_b.id,
                    proposed_by="lead",
                    status="pending",
                )
            )
    async with await h.client(lead_a) as client:
        response = await client.post(
            f"/api/v1/projects/{team_a.id}/swaps",
            json={"from_user_id": str(a1.id), "to_user_id": str(b1.id)},
        )
    assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_swap_accept_gates(harness: Harness) -> None:
    h = harness
    _, team_a, _, lead_a, _, a1, a2, b1 = await two_teams(h)
    body = await propose(h, team_a, lead_a, a1, b1)
    async with await h.client(a2) as client:
        assert_problem(
            await client.post(f"/api/v1/swaps/{body['id']}/accept"), 403, ErrorCode.FORBIDDEN
        )
        assert_problem(
            await client.post(f"/api/v1/swaps/{uuid4()}/accept"), 404, ErrorCode.NOT_FOUND
        )
    async with h.anonymous() as client:
        assert (await client.post(f"/api/v1/swaps/{body['id']}/accept")).status_code == 401


async def test_swap_project_actions(harness: Harness) -> None:
    h = harness
    _, team_a, _, lead_a, _, _, _, _ = await two_teams(h)
    async with h.db() as session:
        project = await session.get_one(models.Project, team_a.id)
        for state in (TeamStatus.REVIEW, TeamStatus.FORMING, TeamStatus.LOCKED):
            project.team_status = state.value
            actions = projects.project_actions(project, members=[], minimum=2, actor=lead_a)
            assert ("swap" in actions) == (state == TeamStatus.REVIEW)
            assert ("merge" in actions) == (state == TeamStatus.REVIEW)
            if state != TeamStatus.LOCKED:
                assert "invite" in actions
