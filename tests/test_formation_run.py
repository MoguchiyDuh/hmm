from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from factories import (
    accept_invite,
    alerts,
    create_party,
    enrollment_row,
    form_now,
    formation,
    formation_row,
    invite_to_party,
    invites,
    join,
    live_members,
    make_event,
    notices,
    parties,
    payload,
    plan,
    queue_rows,
    runs,
    teams,
)
from harness import Harness

from hmm.formation import jobs


async def test_closed_party_becomes_team_as_is(harness: Harness) -> None:
    h = harness
    owner, member, third, solo = await h.user(), await h.user(), await h.user(), await h.user()
    outsider = await h.user()
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    party = await create_party(h, event, owner)
    for user in (member, third):
        invite = await invite_to_party(h, UUID(party["id"]), owner, user)
        await accept_invite(h, user, UUID(invite["id"]))
    async with await h.client(owner) as client:
        assert (
            await client.patch(f"/api/v1/parties/{party['id']}", json={"closed": True})
        ).status_code == 200
    await invite_to_party(h, UUID(party["id"]), owner, outsider)
    await join(h, event, solo)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    assert {row.user_id for row in await live_members(h, team)} == {
        owner.id,
        member.id,
        third.id,
    }
    assert team.seats_closed_at is not None
    team_rows = await live_members(h, team)
    assert all(row.state == "proposed" for row in team_rows)
    assert team.lead_user_id == owner.id
    assert await parties(h, event) == []
    sent = await notices(h, user=outsider, kind="invite_closed")
    assert len(sent) == 1
    assert payload(sent[0])["reason"] == "formation"
    assert await invites(h, event) == []
    reserve = await enrollment_row(h, event, solo)
    assert reserve is not None
    assert reserve.status == "reserve"
    placed = [await enrollment_row(h, event, user) for user in (owner, member, third)]
    assert all(row.status == "placed" for row in placed)


async def test_open_party_is_never_split(harness: Harness) -> None:
    h = harness
    owner, member, third = await h.user(), await h.user(), await h.user()
    solos = [await h.user(), await h.user()]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=4)
    party = await create_party(h, event, owner)
    for user in (member, third):
        invite = await invite_to_party(h, UUID(party["id"]), owner, user)
        await accept_invite(h, user, UUID(invite["id"]))
    for user in solos:
        await join(h, event, user)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    members = await live_members(h, team)
    assert len(members) == 4
    assert {row.user_id for row in members} >= {owner.id, member.id, third.id}
    assert team.lead_user_id == owner.id
    queued = [row for row in await queue_rows(h, event) if row.status == "reserve"]
    assert len(queued) == 1


async def test_team_sizes_stay_within_bounds(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(8)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    for user in members:
        await join(h, event, user)
    await form_now(h, event, admin)
    sizes = sorted([len(await live_members(h, team)) for team in await teams(h, event)])
    assert sizes == [4, 4]
    placements = [await enrollment_row(h, event, user) for user in members]
    assert {row.status for row in placements} == {"placed"}
    proposed = sum([len(await live_members(h, team)) for team in await teams(h, event)])
    assert proposed == 8


async def test_accept_by_is_capped_by_start(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    early = [await h.user() for _ in range(3)]
    event = await make_event(h, minimum=3, maximum=3)
    for user in early:
        await join(h, event, user)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    team_rows = await live_members(h, team)
    assert all(row.accept_by == h.now + timedelta(hours=24) for row in team_rows)
    late = [await h.user() for _ in range(3)]
    starts_at = h.now + timedelta(hours=10)
    soon = await make_event(
        h,
        minimum=3,
        maximum=3,
        starts_in=timedelta(hours=10),
        registration_deadline=h.now + timedelta(hours=2),
    )
    for user in late:
        await join(h, soon, user)
    await form_now(h, soon, admin)
    [soon_team] = await teams(h, soon)
    assert {row.accept_by for row in await live_members(h, soon_team)} == {starts_at}


async def test_queued_members_become_reserve(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(7)]
    admin = await h.admin()
    event = await make_event(h, minimum=4, maximum=5)
    for user in members:
        await join(h, event, user)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    assert len(await live_members(h, team)) == 5
    placed = [user for user in members if (await enrollment_row(h, event, user)).status == "placed"]
    reserved = [
        user for user in members if (await enrollment_row(h, event, user)).status == "reserve"
    ]
    assert len(placed) == 5
    assert len(reserved) == 2
    for user in placed:
        assert [notice.user_id for notice in await notices(h, user=user, kind="team_proposed")] == [
            user.id
        ]
    for user in reserved:
        sent = await notices(h, user=user, kind="reserve_placed")
        assert [notice.user_id for notice in sent] == [user.id]
    for user in reserved:
        assert await notices(h, user=user, kind="team_proposed") == []


async def test_lead_is_biggest_party_owner(harness: Harness) -> None:
    h = harness
    big_owner, big_member, big_third = await h.user(), await h.user(), await h.user()
    small_owner, small_member = await h.user(), await h.user()
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    big = await create_party(h, event, big_owner)
    for user in (big_member, big_third):
        invite = await invite_to_party(h, UUID(big["id"]), big_owner, user)
        await accept_invite(h, user, UUID(invite["id"]))
    small = await create_party(h, event, small_owner)
    invite = await invite_to_party(h, UUID(small["id"]), small_owner, small_member)
    await accept_invite(h, small_member, UUID(invite["id"]))
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    assert team.lead_user_id == big_owner.id
    assert (await formation_row(h, event)).phase == "formed"


async def test_lead_ties_use_earliest_party(harness: Harness) -> None:
    h = harness
    first_owner, first_member = await h.user(), await h.user()
    second_owner, second_member = await h.user(), await h.user()
    solo = await h.user()
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    first = await create_party(h, event, first_owner)
    invite = await invite_to_party(h, UUID(first["id"]), first_owner, first_member)
    await accept_invite(h, first_member, UUID(invite["id"]))
    h.advance(timedelta(minutes=1))
    second = await create_party(h, event, second_owner)
    invite = await invite_to_party(h, UUID(second["id"]), second_owner, second_member)
    await accept_invite(h, second_member, UUID(invite["id"]))
    await join(h, event, solo)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    assert team.lead_user_id == first_owner.id


async def test_lead_prefers_willing_then_earliest(harness: Harness) -> None:
    h = harness
    first, willing, third = await h.user(), await h.user(), await h.user()
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=3)
    await join(h, event, first)
    h.advance(timedelta(minutes=1))
    await join(h, event, willing, willing_to_lead=True)
    h.advance(timedelta(minutes=1))
    await join(h, event, third)
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    assert team.lead_user_id == willing.id
    quiet_first, quiet_second, quiet_third = await h.user(), await h.user(), await h.user()
    quiet = await make_event(h, minimum=3, maximum=3)
    for user in (quiet_second, quiet_first, quiet_third):
        await join(h, quiet, user)
        h.advance(timedelta(minutes=1))
    await form_now(h, quiet, admin)
    [quiet_team] = await teams(h, quiet)
    assert quiet_team.lead_user_id == quiet_second.id


async def test_valid_algo_plan_is_applied(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    members = [await h.user() for _ in range(6)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    for user in members:
        await join(h, event, user)
        h.advance(timedelta(seconds=1))
    units = [row.id for row in await queue_rows(h, event)]
    requested: list[Any] = []

    async def fake(ctx, request):
        requested.append(request.units)
        return plan([units[0], units[1], units[5]], [units[2], units[3], units[4]])

    monkeypatch.setattr(h.algorithm, "form_teams", fake)
    await form_now(h, event, admin)
    [run] = await runs(h, event, admin)
    assert run["status"] == "applied"
    assert run["planner"] == "algo"
    assert run["validation_errors"] == []
    assert run["output"]["algo"] is not None
    assert len(requested) == 1
    found = set()
    for team in await teams(h, event):
        team_rows = await live_members(h, team)
        found.add(frozenset(row.user_id for row in team_rows))
    assert found == {
        frozenset({members[0].id, members[1].id, members[5].id}),
        frozenset({members[2].id, members[3].id, members[4].id}),
    }


@pytest.mark.parametrize("case", ["duplicate", "bounds", "fewer"])
async def test_invalid_algo_plan_falls_back(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    h = harness
    members = [await h.user() for _ in range(7 if case == "duplicate" else 6)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    for user in members:
        await join(h, event, user)
        h.advance(timedelta(seconds=1))
    units = [row.id for row in await queue_rows(h, event)]
    plans = {
        "duplicate": lambda: plan([units[0], units[0], units[1], units[2]], units[3:]),
        "bounds": lambda: plan(units),
        "fewer": lambda: plan(units[:3]),
    }

    async def fake(ctx, request):
        return plans[case]()

    monkeypatch.setattr(h.algorithm, "form_teams", fake)
    await form_now(h, event, admin)
    [run] = await runs(h, event, admin)
    assert run["status"] == "applied"
    assert run["planner"] == "fallback"
    assert run["output"]["algo"] is not None
    expected = {
        "duplicate": "Duplicate unit",
        "bounds": "outside bounds",
        "fewer": "fewer people than pack",
    }[case]
    assert any(expected in error for error in run["validation_errors"])
    [alert] = await alerts(h, "formation_failed")
    assert alert.status == "open"
    placed = sum([len(await live_members(h, team)) for team in await teams(h, event)])
    sizes = sorted([len(await live_members(h, team)) for team in await teams(h, event)])
    assert placed == len(members)
    assert all(3 <= size <= 5 for size in sizes)


async def test_algo_failure_falls_back_to_pack(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    members = [await h.user() for _ in range(6)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    for user in members:
        await join(h, event, user)

    async def broken(ctx, request):
        raise RuntimeError("algo down")

    monkeypatch.setattr(h.algorithm, "form_teams", broken)
    await form_now(h, event, admin)
    [run] = await runs(h, event, admin)
    assert run["planner"] == "fallback"
    assert run["output"]["algo"] is None
    assert any("RuntimeError" in error for error in run["validation_errors"])
    sizes = sorted([len(await live_members(h, team)) for team in await teams(h, event)])
    assert sizes == [3, 3]
    assert len(await alerts(h, "formation_failed")) == 1


async def test_run_record_contents(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(3)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=3)
    for user in members:
        await join(h, event, user)
    await form_now(h, event, admin)
    [run] = await runs(h, event, admin)
    assert run["status"] == "applied"
    assert run["planner"] == "algo"
    assert run["applied_at"] == h.now.isoformat().replace("+00:00", "Z")
    assert len(run["input"]["units"]) == 3
    assert run["input"]["request"]["target"] == 3
    assert run["input"]["request"]["units"]
    assert len(run["output"]["packing"]["new_teams"]) == 1
    formation = await formation_row(h, event)
    assert formation.last_run_id == UUID(run["id"])
    assert formation.phase == "formed"


async def test_job_failure_returns_to_open(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    members = [await h.user() for _ in range(3)]
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=3)
    for user in members:
        await join(h, event, user)

    async def broken(*args, **kwargs):
        raise RuntimeError("apply exploded")

    monkeypatch.setattr(jobs, "apply_run", broken)
    await form_now(h, event, admin)
    formation = await formation_row(h, event)
    assert formation.phase == "open"
    assert formation.formation_at == h.now + timedelta(hours=1)
    assert await teams(h, event) == []
    [run] = await runs(h, event, admin)
    assert run["status"] == "failed"
    assert any("apply exploded" in error for error in run["validation_errors"])
    [alert] = await alerts(h, "formation_failed")
    assert alert.status == "open"


async def test_empty_event_goes_formed(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event = await make_event(h, minimum=3, maximum=5)
    await form_now(h, event, admin)
    assert (await formation_row(h, event)).phase == "formed"
    assert await teams(h, event) == []
    view = await formation(h, event, admin)
    assert view["phase"] == "formed"
    assert view["counts"] == {"queued": 0, "parties": 0, "teams": 0}
    [run] = await runs(h, event, admin)
    assert run["status"] == "applied"
