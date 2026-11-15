from datetime import timedelta
from uuid import UUID

from formation_support import (
    accept_invite,
    create_party,
    enrollment_row,
    formation,
    invite_to_party,
    invites,
    join,
    make_event,
    maybe_enrollment,
    notices,
    parties,
    seed_enrollment,
    seed_team,
    set_phase,
    teams,
)
from harness import Harness
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

from hmm import models
from hmm.common import ErrorCode


async def test_enroll_update_withdraw_and_reenroll(harness: Harness) -> None:
    h = harness
    member, other = await h.user(login="member"), await h.user(login="other")
    event = await make_event(h)
    await join(h, event, other)
    body = await join(
        h, event, member, preferred_area_key="backend", note="hello", willing_to_lead=True
    )
    assert body["status"] == "active"
    assert body["willing_to_lead"] is True
    assert body["preferred_area_id"] is not None
    async with await h.client(member) as client:
        patched = await client.patch(
            f"/api/v1/events/{event.id}/enroll",
            json={"preferred_area_key": None, "note": "bye", "willing_to_lead": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["preferred_area_id"] is None
        assert patched.json()["note"] == "bye"
        assert patched.json()["willing_to_lead"] is False
        mine = await client.get("/api/v1/me/enrollments", params={"status": "active"})
        assert [item["id"] for item in mine.json()] == [body["id"]]
        listed = await client.get(f"/api/v1/events/{event.id}/enrollments")
        assert listed.status_code == 200, listed.text
        assert {item["user_id"] for item in listed.json()["items"]} == {
            str(member.id),
            str(other.id),
        }
        withdrawn = await client.delete(f"/api/v1/events/{event.id}/enroll")
        assert withdrawn.status_code == 204, withdrawn.text
    row = await enrollment_row(h, event, member)
    assert row is not None
    assert row.status == "withdrawn"
    assert row.status_at == h.now
    h.advance(timedelta(hours=1))
    again = await join(h, event, member)
    assert again["id"] == body["id"]
    row = await enrollment_row(h, event, member)
    assert row is not None
    assert row.status == "active"
    assert row.status_at == h.now


async def test_enroll_conflicts(harness: Harness) -> None:
    h = harness
    a, b, c, late = await h.user(), await h.user(), await h.user(), await h.user()
    event = await make_event(
        h, registration_deadline=h.now + timedelta(days=2), starts_in=timedelta(days=5)
    )
    admin = await h.admin()
    for user in (a, b, c):
        await join(h, event, user)
    async with await h.client(a) as client:
        assert_problem(
            await client.post(f"/api/v1/events/{event.id}/enroll", json={}),
            409,
            ErrorCode.ALREADY_ENROLLED,
        )
    async with await h.client(admin) as client:
        assert (
            await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
        ).status_code == 202
    await h.run_jobs()
    async with await h.client(b) as client:
        assert_problem(
            await client.post(f"/api/v1/events/{event.id}/enroll", json={}),
            409,
            ErrorCode.ALREADY_IN_TEAM,
        )
    h.advance(timedelta(days=3))
    async with await h.client(late) as client:
        assert_problem(
            await client.post(f"/api/v1/events/{event.id}/enroll", json={}),
            409,
            ErrorCode.ENROLLMENT_CLOSED,
        )


async def test_withdraw_conflicts(harness: Harness) -> None:
    h = harness
    a, b, c = await h.user(), await h.user(), await h.user()
    event = await make_event(h, minimum=3, maximum=3, starts_in=timedelta(days=5))
    await join(h, event, a)
    async with await h.client(b) as client:
        assert_problem(
            await client.delete(f"/api/v1/events/{event.id}/enroll"),
            409,
            ErrorCode.NOT_ENROLLED,
        )
    await join(h, event, b)
    await join(h, event, c)
    admin = await h.admin()
    async with await h.client(admin) as client:
        assert (
            await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
        ).status_code == 202
    await h.run_jobs()
    async with await h.client(a) as client:
        assert_problem(
            await client.delete(f"/api/v1/events/{event.id}/enroll"),
            409,
            ErrorCode.ALREADY_IN_TEAM,
        )
    h.advance(timedelta(days=6))
    async with await h.client(b) as client:
        assert_problem(
            await client.delete(f"/api/v1/events/{event.id}/enroll"),
            409,
            ErrorCode.ENROLLMENT_CLOSED,
        )


async def test_withdraw_leaves_party_and_deletes_last(harness: Harness) -> None:
    h = harness
    owner, member = await h.user(), await h.user()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, member)
    await accept_invite(h, member, UUID(invite["id"]))
    assert len(await parties(h, event)) == 1
    async with await h.client(owner) as client:
        assert (await client.delete(f"/api/v1/events/{event.id}/enroll")).status_code == 204
    [remaining] = await parties(h, event)
    assert remaining.owner_user_id == member.id
    assert (await enrollment_row(h, event, owner)).party_id is None
    async with await h.client(member) as client:
        assert (await client.delete(f"/api/v1/events/{event.id}/enroll")).status_code == 204
    assert await parties(h, event) == []
    assert (await enrollment_row(h, event, member)).status_at == h.now


async def test_queue_and_party_mutations_blocked_while_forming(harness: Harness) -> None:
    h = harness
    owner, member, solo, new = await h.user(), await h.user(), await h.user(), await h.user()
    invitee = await h.user()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, member)
    await accept_invite(h, member, UUID(invite["id"]))
    pending = await invite_to_party(h, UUID(party["id"]), owner, invitee)
    await join(h, event, solo)
    await join(h, event, new)
    await seed_enrollment(h, event, invitee)
    await set_phase(h, event, "forming")
    async with await h.client(new) as client:
        assert_problem(
            await client.post(f"/api/v1/events/{event.id}/enroll", json={}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.patch(f"/api/v1/events/{event.id}/enroll", json={"note": "x"}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.delete(f"/api/v1/events/{event.id}/enroll"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(f"/api/v1/events/{event.id}/party"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
    async with await h.client(owner) as client:
        assert_problem(
            await client.patch(f"/api/v1/parties/{party['id']}", json={"closed": False}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(
                f"/api/v1/parties/{party['id']}/invites", json={"user_id": str(invitee.id)}
            ),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(f"/api/v1/parties/{party['id']}/leave"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.delete(f"/api/v1/parties/{party['id']}/members/{member.id}"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
    async with await h.client(invitee) as client:
        assert_problem(
            await client.post(f"/api/v1/invites/{pending['id']}/accept"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(f"/api/v1/invites/{pending['id']}/decline"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
    async with await h.client(owner) as client:
        assert_problem(
            await client.delete(f"/api/v1/invites/{pending['id']}"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )


async def test_team_mutations_blocked_while_forming(harness: Harness) -> None:
    h = harness
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    event = await make_event(h)
    team = await seed_team(
        h, event, lead, (member,), state="proposed", accept_by=h.now + timedelta(hours=1)
    )
    await seed_enrollment(h, event, lead, status="placed")
    await seed_enrollment(h, event, outsider)
    await set_phase(h, event, "forming")
    async with await h.client(member) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{team.id}/placement/accept"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(f"/api/v1/projects/{team.id}/placement/decline", json={}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(f"/api/v1/projects/{team.id}/leave", json={}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
    async with await h.client(lead) as client:
        assert_problem(
            await client.patch(f"/api/v1/projects/{team.id}/seats", json={"closed": True}),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(
                f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(lead.id)}
            ),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.patch(
                f"/api/v1/projects/{team.id}/members/{member.id}", json={"area_key": "backend"}
            ),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.post(
                f"/api/v1/projects/{team.id}/invites", json={"user_id": str(outsider.id)}
            ),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        assert_problem(
            await client.delete(f"/api/v1/projects/{team.id}/members/{member.id}"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )


async def test_create_party_enrolls_owner_and_grants_view(harness: Harness) -> None:
    h = harness
    owner, outsider = await h.user(), await h.user()
    admin = await h.admin()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    assert party["owner_user_id"] == str(owner.id)
    assert party["closed"] is False
    assert [item["user_id"] for item in party["members"]] == [str(owner.id)]
    row = await enrollment_row(h, event, owner)
    assert row is not None
    assert row.status == "active"
    assert str(row.party_id) == party["id"]
    again = await create_party(h, event, owner)
    assert again["id"] == party["id"]
    async with await h.client(outsider) as client:
        assert_problem(await client.get(f"/api/v1/parties/{party['id']}"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(admin) as client:
        assert (await client.get(f"/api/v1/parties/{party['id']}")).status_code == 200


async def test_party_invite_eligibility(harness: Harness) -> None:
    h = harness
    owner, partner = await h.user(), await h.user()
    outsider = await h.user()
    pending_user = await h.user(onboarded=False)
    banned = await h.user(banned_at=h.now)
    dnd = await h.user(dnd_indefinite=True)
    on_team = await h.user()
    event = await make_event(
        h, registration_deadline=h.now + timedelta(days=3), starts_in=timedelta(days=5)
    )
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, partner)
    assert invite["status"] == "pending"
    assert invite["expires_at"] == (h.now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    sent = await notices(h, user=partner, kind="party_invite")
    assert len(sent) == 1
    async with await h.client(owner) as client:
        for user, status, code in (
            (pending_user, 403, ErrorCode.ONBOARDING_REQUIRED),
            (banned, 403, ErrorCode.BANNED),
            (dnd, 409, ErrorCode.USER_DND),
        ):
            assert_problem(
                await client.post(
                    f"/api/v1/parties/{party['id']}/invites", json={"user_id": str(user.id)}
                ),
                status,
                code,
            )
        assert_problem(
            await client.post(
                f"/api/v1/parties/{party['id']}/invites", json={"user_id": str(owner.id)}
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with await h.client(outsider) as client:
        assert_problem(
            await client.post(
                f"/api/v1/parties/{party['id']}/invites", json={"user_id": str(outsider.id)}
            ),
            403,
            ErrorCode.FORBIDDEN,
        )
    await seed_team(h, event, on_team)
    async with await h.client(owner) as client:
        assert_problem(
            await client.post(
                f"/api/v1/parties/{party['id']}/invites", json={"user_id": str(on_team.id)}
            ),
            409,
            ErrorCode.ALREADY_IN_TEAM,
        )


async def test_invite_accept_moves_between_parties(harness: Harness) -> None:
    h = harness
    first, second, mover = await h.user(), await h.user(), await h.user()
    event = await make_event(h)
    host = await create_party(h, event, first)
    await invite_to_party(h, UUID(host["id"]), first, second)
    previous = await create_party(h, event, mover)
    assert len(await parties(h, event)) == 2
    invite = await invite_to_party(h, UUID(host["id"]), first, mover)
    await accept_invite(h, mover, UUID(invite["id"]))
    assert [party.id for party in await parties(h, event)] == [UUID(host["id"])]
    row = await enrollment_row(h, event, mover)
    assert row is not None
    assert str(row.party_id) == host["id"]
    assert previous["owner_user_id"] == str(mover.id)


async def test_party_full(harness: Harness) -> None:
    h = harness
    owner, second, third, extra = await h.user(), await h.user(), await h.user(), await h.user()
    event = await make_event(h, minimum=2, maximum=3)
    party = await create_party(h, event, owner)
    for user in (second, third, extra):
        await invite_to_party(h, UUID(party["id"]), owner, user)
    pending = await invites(h, event)
    by_user = {invite.user_id: invite.id for invite in pending}
    await accept_invite(h, second, by_user[second.id])
    await accept_invite(h, third, by_user[third.id])
    async with await h.client(extra) as client:
        assert_problem(
            await client.post(f"/api/v1/invites/{by_user[extra.id]}/accept"),
            409,
            ErrorCode.PARTY_FULL,
        )
    row = await maybe_enrollment(h, event, extra)
    assert row is None
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Enrollment)
                .where(models.Enrollment.party_id == UUID(party["id"]))
            )
            == 3
        )


async def test_party_leave_passes_ownership_and_deletes(harness: Harness) -> None:
    h = harness
    owner, early, late = await h.user(), await h.user(), await h.user()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    early_invite = await invite_to_party(h, UUID(party["id"]), owner, early)
    await accept_invite(h, early, UUID(early_invite["id"]))
    late_invite = await invite_to_party(h, UUID(party["id"]), owner, late)
    await accept_invite(h, late, UUID(late_invite["id"]))
    async with await h.client(owner) as client:
        assert (await client.post(f"/api/v1/parties/{party['id']}/leave")).status_code == 204
    [remaining] = await parties(h, event)
    assert remaining.owner_user_id == early.id
    async with await h.client(late) as client:
        assert (await client.post(f"/api/v1/parties/{party['id']}/leave")).status_code == 204
    [remaining] = await parties(h, event)
    assert remaining.owner_user_id == early.id
    async with await h.client(early) as client:
        assert (await client.post(f"/api/v1/parties/{party['id']}/leave")).status_code == 204
    assert await parties(h, event) == []
    assert (await enrollment_row(h, event, early)).party_id is None


async def test_party_kick_is_owner_only(harness: Harness) -> None:
    h = harness
    owner, member, other = await h.user(), await h.user(), await h.user()
    event = await make_event(h)
    party = await create_party(h, event, owner)
    for user in (member, other):
        invite = await invite_to_party(h, UUID(party["id"]), owner, user)
        await accept_invite(h, user, UUID(invite["id"]))
    async with await h.client(member) as client:
        assert_problem(
            await client.delete(f"/api/v1/parties/{party['id']}/members/{other.id}"),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with await h.client(owner) as client:
        assert_problem(
            await client.delete(f"/api/v1/parties/{party['id']}/members/{owner.id}"),
            403,
            ErrorCode.FORBIDDEN,
        )
        assert (
            await client.delete(f"/api/v1/parties/{party['id']}/members/{member.id}")
        ).status_code == 204
    row = await enrollment_row(h, event, member)
    assert row is not None
    assert row.party_id is None
    assert row.status == "active"
    assert (await enrollment_row(h, event, other)).party_id is not None


async def test_party_closed_requires_min_and_reopens(harness: Harness) -> None:
    h = harness
    owner, member, late = await h.user(), await h.user(), await h.user()
    event = await make_event(h, minimum=3, maximum=5)
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, member)
    await accept_invite(h, member, UUID(invite["id"]))
    async with await h.client(owner) as client:
        assert_problem(
            await client.patch(f"/api/v1/parties/{party['id']}", json={"closed": True}),
            409,
            ErrorCode.PARTY_TOO_SMALL,
        )
        late_invite = await invite_to_party(h, UUID(party["id"]), owner, late)
        await accept_invite(h, late, UUID(late_invite["id"]))
        closed = await client.patch(f"/api/v1/parties/{party['id']}", json={"closed": True})
        assert closed.status_code == 200, closed.text
        assert closed.json()["closed"] is True
        assert (await client.post(f"/api/v1/parties/{party['id']}/leave")).status_code == 204
        assert (await enrollment_row(h, event, owner)).party_id is None
        [remaining] = await parties(h, event)
        assert remaining.closed is False


async def test_formation_view_projects_queue(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(7)]
    event = await make_event(h, minimum=3, maximum=5)
    for user in members:
        await join(h, event, user)
    view = await formation(h, event, members[0])
    assert view["phase"] == "open"
    assert (view["target"], view["min"], view["max"]) == (5, 3, 5)
    assert view["counts"] == {"queued": 7, "parties": 0, "teams": 0}
    assert sorted(len(team) for team in view["projection"]["new_teams"]) == [3, 4]
    assert view["my_enrollment"]["user_id"] == str(members[0].id)
    assert set(view["allowed_actions"]) == {"withdraw", "update_enrollment", "create_party"}
    outsider = await h.user()
    anon_view = await formation(h, event, outsider)
    assert anon_view["my_enrollment"] is None
    assert set(anon_view["allowed_actions"]) == {"enroll", "create_party"}
    closed_owner, others = members[0], members[1:3]
    party = await create_party(h, event, closed_owner)
    for user in others:
        invite = await invite_to_party(h, UUID(party["id"]), closed_owner, user)
        await accept_invite(h, user, UUID(invite["id"]))
    async with await h.client(closed_owner) as client:
        closed = await client.patch(f"/api/v1/parties/{party['id']}", json={"closed": True})
        assert closed.status_code == 200, closed.text
    view = await formation(h, event, closed_owner)
    assert [party["id"]] in view["projection"]["new_teams"]
    assert view["counts"]["parties"] == 1
    assert view["counts"]["queued"] == 7
    assert all(party["id"] not in team for team in view["projection"]["new_teams"][1:])


async def test_formation_view_keeps_open_party_whole(harness: Harness) -> None:
    h = harness
    owner, member = await h.user(), await h.user()
    solos = [await h.user() for _ in range(3)]
    event = await make_event(h, minimum=3, maximum=5)
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, member)
    await accept_invite(h, member, UUID(invite["id"]))
    for user in solos:
        await join(h, event, user)
    view = await formation(h, event, owner)
    [team] = view["projection"]["new_teams"]
    assert len(team) == 4
    assert team.count(party["id"]) == 1
    assert view["counts"] == {"queued": 5, "parties": 1, "teams": 0}


async def test_enrollment_listing_includes_teams(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(3)]
    event = await make_event(h, minimum=3, maximum=3)
    for user in members:
        await join(h, event, user)
    admin = await h.admin()
    async with await h.client(admin) as client:
        assert (
            await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
        ).status_code == 202
    await h.run_jobs()
    [team] = await teams(h, event)
    async with await h.client(members[0]) as client:
        listed = await client.get(f"/api/v1/events/{event.id}/enrollments")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert {item["status"] for item in body["items"]} == {"placed"}
        assert [item["id"] for item in body["teams"]] == [str(team.id)]
        assert {item["user_id"] for item in body["teams"][0]["members"]} == {
            str(user.id) for user in members
        }
        mine = await client.get("/api/v1/me/enrollments", params={"status": "placed"})
        assert [item["user_id"] for item in mine.json()] == [str(members[0].id)]
