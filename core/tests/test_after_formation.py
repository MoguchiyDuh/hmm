import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

from formation_support import (
    accept_invite,
    alerts,
    create_party,
    enrollment_row,
    form_now,
    formation_row,
    formed,
    invite_to_party,
    invites,
    join,
    live_members,
    make_event,
    notices,
    parties,
    run_tick,
    stored_member,
    teams,
)
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import select
from test_api_skeleton import assert_problem


async def placement(
    h: Harness, user: models.User, team: models.Project, action: str, **body: Any
) -> Any:
    async with await h.client(user) as client:
        return await client.post(f"/api/v1/projects/{team.id}/placement/{action}", json=body)


async def accept(h: Harness, user: models.User, team: models.Project) -> Any:
    response = await placement(h, user, team, "accept")
    assert response.status_code == 200, response.text
    return response.json()


async def decline(
    h: Harness, user: models.User, team: models.Project, *, withdraw: bool = False
) -> Any:
    response = await placement(h, user, team, "decline", withdraw=withdraw)
    assert response.status_code == 200, response.text
    return response.json()


async def leave(
    h: Harness, user: models.User, team: models.Project, *, withdraw: bool = False
) -> Any:
    async with await h.client(user) as client:
        response = await client.post(
            f"/api/v1/projects/{team.id}/leave", json={"withdraw": withdraw}
        )
    assert response.status_code == 204, response.text


async def kick(h: Harness, actor: models.User, team: models.Project, user: models.User) -> Any:
    async with await h.client(actor) as client:
        return await client.delete(f"/api/v1/projects/{team.id}/members/{user.id}")


async def team_invite(
    h: Harness, actor: models.User, team: models.Project, user: models.User
) -> Any:
    async with await h.client(actor) as client:
        return await client.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(user.id)}
        )


async def confirm_all(h: Harness, users: list[models.User], team: models.Project) -> None:
    for user in users:
        await accept(h, user, team)


async def test_accept_confirms_and_notifies_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    lead = team.lead_user_id
    response = await accept(h, users[1], team)
    assert response["lead_user_id"] == str(lead)
    member = await stored_member(h, team, users[1])
    assert member.state == "confirmed"
    assert member.confirmed_at == h.now
    assert member.accept_by is None
    lead_notices = await notices(
        h, user=next(user for user in users if user.id == lead), kind="team_member_joined"
    )
    assert len(lead_notices) == 1
    await accept(h, users[1], team)
    lead_notices = await notices(
        h, user=next(user for user in users if user.id == lead), kind="team_member_joined"
    )
    assert len(lead_notices) == 1


async def test_accept_after_deadline_lapses(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    async with h.db() as session:
        row = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == team.id,
                models.ProjectMember.user_id == users[2].id,
            )
        )
        assert row is not None
        row.accept_by = h.now - timedelta(seconds=1)
    response = await placement(h, users[2], team, "accept")
    assert_problem(response, 409, ErrorCode.INVALID_STATE)
    member = await stored_member(h, team, users[2])
    assert member.left_reason == "lapsed"
    enrollment = await enrollment_row(h, event, users[2])
    assert enrollment.status == "lapsed"
    assert len(await notices(h, user=users[2], kind="placement_lapsed")) == 1


async def test_decline_requeues_or_withdraws(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    h.advance(timedelta(minutes=5))
    await decline(h, users[1], team)
    member = await stored_member(h, team, users[1])
    assert member.left_reason == "declined"
    enrollment = await enrollment_row(h, event, users[1])
    assert enrollment.status == "reserve"
    assert enrollment.status_at == h.now
    assert {row.user_id for row in await live_members(h, team)} == {users[0].id, users[2].id}
    recipients = {notice.user_id for notice in await notices(h, kind="team_member_left")}
    assert recipients == {users[0].id, users[2].id}
    await decline(h, users[2], team, withdraw=True)
    assert (await enrollment_row(h, event, users[2])).status == "withdrawn"


async def test_lead_edits_rename_and_areas(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    async with await h.client(users[0]) as client:
        renamed = await client.patch(f"/api/v1/projects/{team.id}", json={"name": "Renamed"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Renamed"
        area = await client.patch(
            f"/api/v1/projects/{team.id}/members/{users[1].id}", json={"area_key": "backend"}
        )
        assert area.status_code == 200, area.text
        member = next(
            item for item in area.json()["members"] if item["user_id"] == str(users[1].id)
        )
        assert member["area_id"] is not None
        unknown = await client.patch(
            f"/api/v1/projects/{team.id}/members/{users[1].id}", json={"area_key": "nope"}
        )
        assert_problem(unknown, 422, ErrorCode.VALIDATION_ERROR)
    async with await h.client(users[1]) as client:
        denied = await client.patch(f"/api/v1/projects/{team.id}", json={"name": "Nope"})
        assert_problem(denied, 403, ErrorCode.FORBIDDEN)


async def test_transfer_lead_is_immediate(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    async with await h.client(users[0]) as client:
        response = await client.post(
            f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(users[1].id)}
        )
        assert response.status_code == 200, response.text
        assert response.json()["lead_user_id"] == str(users[1].id)
    assert (await stored_member(h, team, users[0])).role == "member"
    assert (await stored_member(h, team, users[1])).role == "lead"
    assigned = await notices(h, user=users[1], kind="lead_assigned")
    assert len(assigned) == 1
    async with await h.client(users[0]) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(users[0].id)}
            ),
            403,
            ErrorCode.FORBIDDEN,
        )


async def test_transfer_lead_requires_confirmed(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await accept(h, users[0], team)
    await accept(h, users[1], team)
    async with await h.client(users[0]) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(users[2].id)}
            ),
            409,
            ErrorCode.INVALID_STATE,
        )


async def test_kick_sends_member_to_queue(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    async with await h.client(users[1]) as client:
        assert_problem(
            await client.delete(f"/api/v1/projects/{team.id}/members/{users[2].id}"),
            403,
            ErrorCode.FORBIDDEN,
        )
    response = await kick(h, users[0], team, users[1])
    assert response.status_code == 204, response.text
    assert (await session_team(h, team)).lead_user_id == users[0].id
    member = await stored_member(h, team, users[1])
    assert member.left_reason == "removed"
    assert (await enrollment_row(h, event, users[1])).status == "reserve"
    assert {row.user_id for row in await live_members(h, team)} == {users[0].id, users[2].id}
    async with await h.client(users[0]) as client:
        assert_problem(
            await client.delete(f"/api/v1/projects/{team.id}/members/{users[0].id}"),
            409,
            ErrorCode.INVALID_STATE,
        )


async def test_team_invites_ignore_closed_seats_and_stop_at_max(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(
        h, admin, registration_deadline=h.now + timedelta(days=2), starts_in=timedelta(days=5)
    )
    await confirm_all(h, users, team)
    invited = [await h.user() for _ in range(3)]
    async with await h.client(users[0]) as client:
        closed = await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": True})
        assert closed.status_code == 200, closed.text
    before = len(await notices(h, user=users[0], kind="team_member_joined"))
    first = await team_invite(h, users[0], team, invited[0])
    assert first.status_code == 201, first.text
    await accept_invite(h, invited[0], UUID(first.json()["id"]))
    assert (await stored_member(h, team, invited[0])).state == "confirmed"
    second = await team_invite(h, users[0], team, invited[1])
    await accept_invite(h, invited[1], UUID(second.json()["id"]))
    assert len(await live_members(h, team)) == 5
    assert len(await notices(h, user=users[0], kind="team_member_joined")) == before + 2
    assert_problem(await team_invite(h, users[0], team, invited[2]), 409, ErrorCode.TEAM_FULL)
    h.advance(timedelta(days=3))
    assert_problem(
        await team_invite(h, users[0], team, invited[2]), 409, ErrorCode.ENROLLMENT_CLOSED
    )


async def test_seats_close_needs_min_and_reopens(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    await kick(h, users[0], team, users[2])
    async with await h.client(users[0]) as client:
        assert_problem(
            await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": True}),
            409,
            ErrorCode.SEATS_CLOSE_BELOW_MIN,
        )
    invite = await team_invite(h, users[0], team, users[2])
    await accept_invite(h, users[2], UUID(invite.json()["id"]))
    async with await h.client(users[0]) as client:
        closed = await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": True})
        assert closed.status_code == 200, closed.text
        assert closed.json()["closed"] is True
        reopened = await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": False})
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["closed"] is False


async def test_leave_returns_to_queue_and_passes_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    await leave(h, users[1], team)
    member = await stored_member(h, team, users[1])
    assert member.left_reason == "left"
    assert (await enrollment_row(h, event, users[1])).status == "reserve"
    assert team.lead_user_id == users[0].id
    h.advance(timedelta(hours=2))
    await leave(h, users[0], team)
    assert (await stored_member(h, team, users[0])).left_reason == "left"
    assert (await formation_row(h, event)).phase == "formed"
    stored = await session_team(h, team)
    assert stored.lead_user_id == users[2].id
    assert len(await notices(h, user=users[2], kind="lead_assigned")) == 1
    await leave(h, users[2], team, withdraw=True)
    assert (await enrollment_row(h, event, users[2])).status == "withdrawn"
    stored = await session_team(h, team)
    assert stored.team_status == "disbanded"
    assert stored.lead_user_id is None


async def session_team(h: Harness, team: models.Project) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, team.id)


async def test_leave_after_start_withdraws(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin, starts_in=timedelta(days=5))
    await confirm_all(h, users, team)
    h.advance(timedelta(days=6))
    await leave(h, users[1], team)
    assert (await enrollment_row(h, event, users[1])).status == "withdrawn"
    outsider = await h.user()
    async with await h.client(outsider) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{team.id}/leave", json={}),
            404,
            ErrorCode.NOT_FOUND,
        )


async def test_lead_passes_on_decline_and_lapse(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await accept(h, users[1], team)
    await accept(h, users[2], team)
    await decline(h, users[0], team)
    stored = await session_team(h, team)
    assert stored.lead_user_id == users[1].id
    assert len(await notices(h, user=users[1], kind="lead_assigned")) == 1
    late_event, late_users, late_team = await formed(h, admin)
    await accept(h, late_users[1], late_team)
    await accept(h, late_users[2], late_team)
    h.advance(timedelta(hours=25))
    await run_tick(h, late_event)
    stored = await session_team(h, late_team)
    assert stored.lead_user_id == late_users[1].id
    assert (await stored_member(h, late_team, late_users[0])).left_reason == "lapsed"
    assert len(await notices(h, user=late_users[1], kind="lead_assigned")) == 1
    assert len(await notices(h, user=late_users[0], kind="placement_lapsed")) == 1


async def test_refill_fills_seats_and_creates_teams(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    late = [await h.user() for _ in range(5)]
    for user in late[:2]:
        await join(h, event, user)
    await run_tick(h, event)
    members = await live_members(h, team)
    assert len(members) == 5
    assert {row.joined_via for row in members if row.user_id in {u.id for u in late[:2]}} == {
        "refill"
    }
    late_states = [(await stored_member(h, team, user)).state for user in late[:2]]
    assert late_states == ["proposed", "proposed"]
    for user in late[2:]:
        await join(h, event, user)
    await run_tick(h, event)
    assert len(await live_members(h, team)) == 5
    fresh = [candidate for candidate in await teams(h, event) if candidate.id != team.id]
    assert len(fresh) == 1
    assert len(await live_members(h, fresh[0])) == 3
    assert (await enrollment_row(h, event, late[2])).status == "placed"


async def test_refill_never_replaces_on_left_team(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    await leave(h, users[1], team)
    newcomers = [await h.user() for _ in range(2)]
    for user in newcomers:
        await join(h, event, user)
    await run_tick(h, event)
    assert {row.user_id for row in await live_members(h, team)} == {
        users[0].id,
        users[2].id,
        newcomers[0].id,
        newcomers[1].id,
    }
    assert (await enrollment_row(h, event, users[1])).status == "reserve"


async def test_ban_removes_from_team_and_passes_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    async with await h.client(admin) as client:
        banned = await client.post(
            f"/api/v1/admin/users/{users[1].id}/ban", json={"reason": "fraud"}
        )
        assert banned.status_code == 200, banned.text
    member = await stored_member(h, team, users[1])
    assert member.left_reason == "banned"
    assert (await enrollment_row(h, event, users[1])).status == "withdrawn"
    assert (await session_team(h, team)).lead_user_id == users[0].id
    async with await h.client(admin) as client:
        banned = await client.post(
            f"/api/v1/admin/users/{users[0].id}/ban", json={"reason": "fraud"}
        )
        assert banned.status_code == 200, banned.text
    assert (await session_team(h, team)).lead_user_id == users[2].id
    assert len(await notices(h, user=users[2], kind="lead_assigned")) == 1


async def test_ban_leaves_party_queue_and_invites(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    owner, partner, invitee = await h.user(), await h.user(), await h.user()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, partner)
    await accept_invite(h, partner, UUID(invite["id"]))
    pending = await invite_to_party(h, UUID(party["id"]), owner, invitee)
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/users/{owner.id}/ban", json={"reason": "fraud"}
        )
        assert response.status_code == 200, response.text
    [remaining] = await parties(h, event)
    assert remaining.owner_user_id == partner.id
    assert (await enrollment_row(h, event, owner)).status == "withdrawn"
    [stored] = [row for row in await invites(h, event) if row.id == UUID(pending["id"])]
    assert stored.status == "closed"
    assert stored.closed_reason == "banned"
    assert len(await notices(h, user=invitee, kind="invite_closed")) == 1


async def test_ban_keeps_active_team_history_and_passes_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        row.team_status = "active"
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/users/{users[0].id}/ban", json={"reason": "fraud"}
        )
        assert response.status_code == 200, response.text
    member = await stored_member(h, team, users[0])
    assert member.left_at is None
    assert (await session_team(h, team)).lead_user_id == users[1].id
    [alert] = await alerts(h, "banned_member_in_active_team")
    assert alert.status == "open"


async def test_attendance_cancels_on_terminal_states_only(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    going = [users[1], users[2]]
    async with h.db() as session:
        for user in going:
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=user.id,
                    status="going",
                    role="attendee",
                    proof_links=[],
                    trust="self",
                    status_at=h.now,
                )
            )
    await decline(h, users[1], team)
    await decline(h, users[2], team, withdraw=True)
    async with h.db() as session:
        statuses = {
            row.user_id: row.status
            for row in await session.scalars(
                select(models.EventAttendance).where(models.EventAttendance.event_id == event.id)
            )
        }
    assert statuses[users[1].id] == "going"
    assert statuses[users[2].id] == "cancelled"


async def test_concurrent_invite_accepts_respect_max(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin, count=4)
    await confirm_all(h, users, team)
    guests = [await h.user(), await h.user()]
    pending = [await team_invite(h, users[0], team, user) for user in guests]
    ids = [UUID(response.json()["id"]) for response in pending]

    async def attempt(user: models.User, invite_id: UUID) -> Any:
        async with await h.client(user) as client:
            return await client.post(f"/api/v1/invites/{invite_id}/accept")

    responses = await asyncio.gather(
        *[attempt(user, invite_id) for user, invite_id in zip(guests, ids, strict=True)]
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert_problem(
        next(response for response in responses if response.status_code == 409),
        409,
        ErrorCode.TEAM_FULL,
    )
    members = await live_members(h, team)
    assert len(members) == 5
    assert len(await teams(h, event)) == 1


async def test_concurrent_invites_never_place_member_twice(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    members = [await h.user() for _ in range(6)]
    for user in members:
        await join(h, event, user)
        h.advance(timedelta(seconds=1))
    await form_now(h, event, admin)
    all_teams = await teams(h, event)
    assert len(all_teams) == 2
    outsider = await h.user()
    leads = {user.id: user for user in members}
    pending = []
    for team in all_teams:
        assert team.lead_user_id is not None
        pending.append(await team_invite(h, leads[team.lead_user_id], team, outsider))
    invite_ids = [UUID(response.json()["id"]) for response in pending]

    async def attempt(invite_id: UUID) -> Any:
        async with await h.client(outsider) as client:
            return await client.post(f"/api/v1/invites/{invite_id}/accept")

    responses = await asyncio.gather(*[attempt(invite_id) for invite_id in invite_ids])
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert_problem(
        next(response for response in responses if response.status_code == 409),
        409,
        ErrorCode.ALREADY_IN_TEAM,
    )
    holding = 0
    for team in all_teams:
        if outsider.id in {row.user_id for row in await live_members(h, team)}:
            holding += 1
    assert holding == 1


async def test_concurrent_refills_place_once(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    await confirm_all(h, users, team)
    late = [await h.user(), await h.user()]
    for user in late:
        await join(h, event, user)
    await asyncio.gather(run_tick(h, event), run_tick(h, event))
    members = await live_members(h, team)
    assert len(members) == 5
    assert len({row.user_id for row in members}) == 5
    placements = [await enrollment_row(h, event, user) for user in late]
    assert {row.status for row in placements} == {"placed"}
    assert len(await teams(h, event)) == 1
