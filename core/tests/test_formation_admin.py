from datetime import timedelta

from formation_support import (
    confirm,
    enrollment_row,
    formation,
    formation_row,
    formed,
    join,
    live_members,
    make_event,
    notices,
    seed_team,
    stored_member,
    teams,
)
from harness import Harness
from hmm_contracts.common import ErrorCode
from test_api_skeleton import assert_problem


async def test_update_formation_settings(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    async with await h.client(admin) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}/formation", json={"target_override": 4}
        )
        assert response.status_code == 200, response.text
        assert response.json()["target"] == 4
        for target in (2, 6):
            assert_problem(
                await client.patch(
                    f"/api/v1/admin/events/{event.id}/formation",
                    json={"target_override": target},
                ),
                422,
                ErrorCode.VALIDATION_ERROR,
            )
        assert (await formation_row(h, event)).target_override == 4
        override = h.now + timedelta(days=3)
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}/formation",
            json={"formation_at_override": override.isoformat()},
        )
        assert response.status_code == 200, response.text
        assert response.json()["formation_at"] == override.isoformat().replace("+00:00", "Z")
        cleared = await client.patch(
            f"/api/v1/admin/events/{event.id}/formation", json={"formation_at_override": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["formation_at"] != response.json()["formation_at"]
    async with await h.client(await h.user()) as client:
        assert_problem(
            await client.patch(
                f"/api/v1/admin/events/{event.id}/formation", json={"target_override": 4}
            ),
            403,
            ErrorCode.FORBIDDEN,
        )


async def test_form_now_only_while_open(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    members = [await h.user() for _ in range(3)]
    event = await make_event(h, minimum=3, maximum=3)
    for user in members:
        await join(h, event, user)
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "pending"
    await h.run_jobs()
    assert (await formation_row(h, event)).phase == "formed"
    async with await h.client(admin) as client:
        assert_problem(
            await client.post(f"/api/v1/admin/events/{event.id}/formation/run"),
            409,
            ErrorCode.INVALID_STATE,
        )
        runs = await client.get(f"/api/v1/admin/events/{event.id}/formation/runs")
        assert runs.status_code == 200, runs.text
        [run] = runs.json()
        assert run["status"] == "applied"
        assert run["applied_at"] is not None


async def test_admin_add_member_ignores_closed_seats(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    for user in users:
        await confirm(h, user, team)
    async with await h.client(users[0]) as client:
        closed = await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": True})
        assert closed.status_code == 200, closed.text
    guests = [await h.user() for _ in range(3)]
    async with await h.client(admin) as client:
        for guest in guests[:2]:
            response = await client.post(
                f"/api/v1/admin/projects/{team.id}/members", json={"user_id": str(guest.id)}
            )
            assert response.status_code == 200, response.text
        assert len(await live_members(h, team)) == 5
        assert_problem(
            await client.post(
                f"/api/v1/admin/projects/{team.id}/members", json={"user_id": str(guests[2].id)}
            ),
            409,
            ErrorCode.TEAM_FULL,
        )
        pending = await client.post(
            f"/api/v1/admin/projects/{team.id}/members",
            json={"user_id": str((await h.user(onboarded=False)).id)},
        )
        assert_problem(pending, 403, ErrorCode.ONBOARDING_REQUIRED)
    member = await stored_member(h, team, guests[0])
    assert member.state == "confirmed"
    assert member.joined_via == "admin"
    assert (await enrollment_row(h, event, guests[0])).status == "placed"


async def test_admin_remove_before_and_after_start(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin, starts_in=timedelta(days=5))
    for user in users:
        await confirm(h, user, team)
    async with await h.client(admin) as client:
        response = await client.delete(f"/api/v1/admin/projects/{team.id}/members/{users[1].id}")
        assert response.status_code == 200, response.text
        assert response.json()["lead_user_id"] == str(users[0].id)
    assert (await stored_member(h, team, users[1])).left_reason == "admin"
    assert (await enrollment_row(h, event, users[1])).status == "reserve"
    h.advance(timedelta(days=6))
    async with await h.client(admin) as client:
        response = await client.delete(f"/api/v1/admin/projects/{team.id}/members/{users[2].id}")
        assert response.status_code == 200, response.text
    assert (await enrollment_row(h, event, users[2])).status == "withdrawn"


async def test_admin_set_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    for user in users:
        await confirm(h, user, team)
    async with await h.client(admin) as client:
        response = await client.put(
            f"/api/v1/admin/projects/{team.id}/lead", json={"user_id": str(users[2].id)}
        )
        assert response.status_code == 200, response.text
        assert response.json()["lead_user_id"] == str(users[2].id)
        assert_problem(
            await client.put(
                f"/api/v1/admin/projects/{team.id}/lead",
                json={"user_id": str((await h.user()).id)},
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
    assert (await stored_member(h, team, users[2])).role == "lead"
    assert (await stored_member(h, team, users[0])).role == "member"
    assert len(await notices(h, user=users[2], kind="lead_assigned")) == 1


async def test_admin_disband(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    for user in users:
        await confirm(h, user, team)
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/projects/{team.id}/disband", json={"reason": "no show"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["team_status"] == "disbanded"
        assert response.json()["lead_user_id"] is None
        again = await client.post(
            f"/api/v1/admin/projects/{team.id}/disband", json={"reason": "no show"}
        )
        assert again.status_code == 200, again.text
    stored = next(row for row in await teams(h, event) if row.id == team.id)
    assert stored.team_status == "disbanded"
    for user in users:
        assert (await stored_member(h, team, user)).left_reason == "disbanded"
        assert (await enrollment_row(h, event, user)).status == "reserve"
        assert len(await notices(h, user=user, kind="team_disbanded")) == 1


async def test_admin_endpoints_forbidden_for_members(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await make_event(h)
    team = await seed_team(h, event, member)
    async with await h.client(member) as client:
        calls = [
            ("patch", f"/api/v1/admin/events/{event.id}/formation", {"target_override": 4}),
            ("post", f"/api/v1/admin/events/{event.id}/formation/run", None),
            ("get", f"/api/v1/admin/events/{event.id}/formation/runs", None),
            ("post", f"/api/v1/admin/projects/{team.id}/members", {"user_id": str(member.id)}),
            ("delete", f"/api/v1/admin/projects/{team.id}/members/{member.id}", None),
            ("put", f"/api/v1/admin/projects/{team.id}/lead", {"user_id": str(member.id)}),
            ("post", f"/api/v1/admin/projects/{team.id}/disband", {"reason": "x"}),
        ]
        for method, path, body in calls:
            kwargs = {} if body is None else {"json": body}
            response = await getattr(client, method)(path, **kwargs)
            assert_problem(response, 403, ErrorCode.FORBIDDEN)


async def test_formation_view_shows_admin_override(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    async with await h.client(admin) as client:
        await client.patch(
            f"/api/v1/admin/events/{event.id}/formation", json={"target_override": 4}
        )
    view = await formation(h, event, admin)
    assert view["target"] == 4
    assert view["min"] == 3
    assert view["max"] == 5
