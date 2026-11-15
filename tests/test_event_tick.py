from datetime import timedelta
from uuid import UUID

from factories import (
    alerts,
    confirm,
    create_party,
    enrollment_row,
    formation_row,
    formed,
    invite_to_party,
    invites,
    join,
    live_members,
    make_event,
    notices,
    payload,
    run_tick,
    stored_member,
    teams,
)
from harness import Harness

from hmm import models


async def run_jobs(h: Harness) -> list[dict]:
    return [job for job in await h.pending_jobs("run_formation") if job["status"] == "todo"]


async def test_tick_enqueues_run_at_formation_time(harness: Harness) -> None:
    h = harness
    event = await make_event(
        h, registration_deadline=h.now + timedelta(hours=100), starts_in=timedelta(days=10)
    )
    for _ in range(2):
        await join(h, event, await h.user())
    h.advance(timedelta(hours=27))
    await run_tick(h, event)
    assert await run_jobs(h) == []
    h.advance(timedelta(hours=1))
    await run_tick(h, event)
    assert len(await run_jobs(h)) == 1
    await run_tick(h, event)
    assert len(await run_jobs(h)) == 1


async def test_tick_sends_formation_soon_once(harness: Harness) -> None:
    h = harness
    users = [await h.user(), await h.user()]
    event = await make_event(
        h, registration_deadline=h.now + timedelta(hours=100), starts_in=timedelta(days=10)
    )
    for user in users:
        await join(h, event, user)
    h.advance(timedelta(hours=3))
    await run_tick(h, event)
    assert await notices(h, kind="formation_soon") == []
    h.advance(timedelta(hours=1))
    await run_tick(h, event)
    sent = await notices(h, kind="formation_soon")
    assert {notice.user_id for notice in sent} == {user.id for user in users}
    assert payload(sent[0])["enrolled_count"] == 2
    assert payload(sent[0])["team_count"] == 2
    await run_tick(h, event)
    assert len(await notices(h, kind="formation_soon")) == 2


async def test_tick_reminds_once_six_hours_before(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _event, users, _team = await formed(h, admin)
    h.advance(timedelta(hours=17, minutes=59))
    await run_tick(h, _event)
    assert await notices(h, kind="accept_reminder") == []
    h.advance(timedelta(minutes=1))
    await run_tick(h, _event)
    sent = await notices(h, kind="accept_reminder")
    assert {notice.user_id for notice in sent} == {user.id for user in users}
    assert payload(sent[0])["hours_remaining"] == 6
    await run_tick(h, _event)
    assert len(await notices(h, kind="accept_reminder")) == 3


async def test_tick_lapses_proposals(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(h, admin)
    h.advance(timedelta(hours=25))
    await run_tick(h, event)
    for user in users:
        member = await stored_member(h, team, user)
        assert member.left_reason == "lapsed"
        assert (await enrollment_row(h, event, user)).status == "lapsed"
        assert len(await notices(h, user=user, kind="placement_lapsed")) == 1
    [stored] = await teams(h, event)
    assert stored.team_status == "disbanded"


async def test_tick_refills_before_reg_deadline_only(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(
        h,
        admin,
        registration_deadline=h.now + timedelta(days=2),
        starts_in=timedelta(days=5),
    )
    for user in users:
        await confirm(h, user, team)
    newcomers = [await h.user() for _ in range(3)]
    for user in newcomers:
        await join(h, event, user)
    h.advance(timedelta(hours=1))
    await run_tick(h, event)
    assert {row.user_id for row in await live_members(h, team)} == {
        users[0].id,
        users[1].id,
        users[2].id,
        newcomers[0].id,
        newcomers[1].id,
    }
    assert (await enrollment_row(h, event, newcomers[2])).status == "reserve"
    h.advance(timedelta(days=2))
    await run_tick(h, event)
    assert (await enrollment_row(h, event, newcomers[2])).status == "reserve"
    assert len(await teams(h, event)) == 1
    assert (await formation_row(h, event)).phase == "formed"


async def test_tick_reg_deadline_expires_invites_and_alerts(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(
        h,
        admin,
        registration_deadline=h.now + timedelta(hours=2),
        starts_in=timedelta(days=5),
    )
    for user in users:
        await confirm(h, user, team)
    outsider = await h.user()
    async with await h.client(users[0]) as client:
        response = await client.post(
            f"/api/v1/projects/{team.id}/invites", json={"user_id": str(outsider.id)}
        )
    assert response.status_code == 201, response.text
    async with await h.client(users[0]) as client:
        assert (
            await client.delete(f"/api/v1/projects/{team.id}/members/{users[2].id}")
        ).status_code == 204
    h.advance(timedelta(hours=3))
    await run_tick(h, event)
    [stored] = await invites(h, event)
    assert stored.status == "expired"
    assert len(await notices(h, user=outsider, kind="invite_expired")) == 1
    [alert] = await alerts(h, "team_below_min")
    assert alert.status == "open"
    assert alert.subject_id == team.id
    assert (await formation_row(h, event)).phase == "formed"
    await run_tick(h, event)
    assert len(await alerts(h, "team_below_min")) == 1


async def test_tick_finalizes_at_start(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, users, team = await formed(
        h,
        admin,
        minimum=3,
        maximum=3,
        registration_deadline=h.now + timedelta(days=2),
        starts_in=timedelta(days=5),
    )
    await confirm(h, users[0], team)
    async with h.db() as session:
        session.add(
            models.Project(
                event_id=event.id,
                name="Empty",
                description_md="",
                goal="",
                links=[],
                visibility="public",
                status="idea",
                team_status="formed",
                origin="formation",
            )
        )
    late = await h.user()
    await join(h, event, late)
    h.advance(timedelta(days=5))
    await run_tick(h, event)
    assert (await formation_row(h, event)).phase == "closed"
    assert (await stored_member(h, team, users[0])).state == "confirmed"
    for user in users[1:]:
        assert (await stored_member(h, team, user)).left_reason == "lapsed"
        assert (await enrollment_row(h, event, user)).status == "lapsed"
    stored = {row.id: row for row in await teams(h, event)}
    assert stored[team.id].team_status == "active"
    assert stored[team.id].lead_user_id == users[0].id
    empty = next(row for row in stored.values() if row.id != team.id)
    assert empty.team_status == "disbanded"
    assert (await enrollment_row(h, event, late)).status == "expired"


async def test_tick_expires_party_invite(harness: Harness) -> None:
    h = harness
    owner, partner = await h.user(), await h.user()
    event = await make_event(
        h, registration_deadline=h.now + timedelta(days=1), starts_in=timedelta(days=5)
    )
    party = await create_party(h, event, owner)
    invite = await invite_to_party(h, UUID(party["id"]), owner, partner)
    h.advance(timedelta(days=2))
    await run_tick(h, event)
    [stored] = await invites(h, event)
    assert stored.id == UUID(invite["id"])
    assert stored.status == "expired"
    assert len(await notices(h, user=partner, kind="invite_expired")) == 1


async def test_tick_is_idempotent(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event, _users, team = await formed(h, admin)
    newcomer = await h.user()
    await join(h, event, newcomer)
    h.advance(timedelta(hours=18))
    await run_tick(h, event)
    first_notices = len(await notices(h))
    first_jobs = await run_jobs(h)
    first_alerts = len(await alerts(h))
    first_members = {row.user_id for row in await live_members(h, team)}
    await run_tick(h, event)
    assert len(await notices(h)) == first_notices
    assert await run_jobs(h) == first_jobs
    assert len(await alerts(h)) == first_alerts
    assert {row.user_id for row in await live_members(h, team)} == first_members
