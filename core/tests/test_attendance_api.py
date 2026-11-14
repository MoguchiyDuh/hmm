from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ActorKind, ConfirmationFactType, ConfirmationSource, ErrorCode
from hmm_contracts.notifications import AttendanceConfirmDigestPayload
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.feed import confirmations
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


async def add_event(
    h: Harness,
    *,
    starts_at: datetime,
    ends_at: datetime,
    status: str = "published",
    creator: models.User | None = None,
) -> models.Event:
    async with h.db() as session:
        row = models.Event(
            slug=f"attendance-{uuid4()}",
            kind="meetup",
            formation_enabled=False,
            title="Attendance meetup",
            starts_at=starts_at,
            ends_at=ends_at,
            timezone="UTC",
            language=[],
            themes=[],
            status=status,
            review_reasons=[],
            origin="member" if creator is not None else "manual",
            created_by=creator.id if creator is not None else None,
            unmapped_areas=[],
        )
        session.add(row)
        await session.flush()
        return row


async def ended_event(h: Harness, **fields: Any) -> models.Event:
    return await add_event(
        h, starts_at=h.now - timedelta(days=1, hours=2), ends_at=h.now - timedelta(days=1), **fields
    )


async def attend(
    h: Harness,
    event: models.Event,
    user: models.User,
    status: str = "attended",
    *,
    status_at: datetime | None = None,
    trust: str = "self",
) -> models.EventAttendance:
    async with h.db() as session:
        row = models.EventAttendance(
            event_id=event.id,
            user_id=user.id,
            status=status,
            role="attendee",
            proof_links=[],
            trust=trust,
            status_at=status_at or h.now,
        )
        session.add(row)
        await session.flush()
        return row


async def recompute_jobs(h: Harness, user: models.User) -> int:
    return len(
        [
            job
            for job in await h.pending_jobs("recompute_member")
            if job["args"] == {"user_id": str(user.id)}
        ]
    )


async def feed_kinds(h: Harness, event: models.Event) -> list[str]:
    async with h.db() as session:
        return sorted(
            await session.scalars(
                select(models.FeedItem.kind).where(models.FeedItem.subject_id == event.id)
            )
        )


async def attendance_rows(h: Harness, event: models.Event) -> list[models.EventAttendance]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.EventAttendance).where(models.EventAttendance.event_id == event.id)
            )
        )


def ids(body: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in body]


async def test_going_creates_row_and_feed_once(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    async with await h.client(member) as c:
        response = await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "going"
        assert body["role"] == "attendee"
        assert body["trust"] == "self"
        assert datetime.fromisoformat(body["status_at"]) == h.now
        assert body["allowed_actions"] == ["edit", "remove"]
        assert body["event"]["id"] == str(event.id)
        assert body["user"]["id"] == str(member.id)
        assert len(await attendance_rows(h, event)) == 1
        assert await recompute_jobs(h, member) == 0
        assert await feed_kinds(h, event) == ["event_going", "event_new"]
        repeat = await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"})
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["id"] == body["id"]
    assert len(await attendance_rows(h, event)) == 1
    assert await feed_kinds(h, event) == ["event_going", "event_new"]
    assert await h.pending_jobs("recompute_member") == []


async def test_going_closes_at_event_end_and_cancelled_events(harness: Harness) -> None:
    h = harness
    member = await h.user()
    involved = await h.user()
    ended = await add_event(h, starts_at=h.now - timedelta(hours=2), ends_at=h.now)
    async with await h.client(member) as c:
        response = await c.put(f"/api/v1/events/{ended.id}/attendance", json={"status": "going"})
        assert_problem(response, 409, ErrorCode.ATTENDANCE_WINDOW_CLOSED)
        h.advance(timedelta(microseconds=1))
        response = await c.put(f"/api/v1/events/{ended.id}/attendance", json={"status": "going"})
        assert_problem(response, 409, ErrorCode.ATTENDANCE_WINDOW_CLOSED)
    event = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    async with await h.client(involved) as c:
        response = await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"})
        assert response.status_code == 200, response.text
    async with h.db() as session:
        await catalog.cancel_event(
            session,
            h.jobs,
            event.id,
            reason="Venue closed",
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            now=h.now,
        )
    async with await h.client(member) as c:
        response = await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(involved) as c:
        response = await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"})
        assert_problem(response, 409, ErrorCode.ATTENDANCE_WINDOW_CLOSED)
        response = await c.put(
            f"/api/v1/events/{event.id}/attendance", json={"status": "cancelled"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["allowed_actions"] == []


async def test_attended_claim_window(harness: Harness) -> None:
    h = harness
    member = await h.user()
    late = await h.user()
    running = await add_event(
        h, starts_at=h.now - timedelta(hours=1), ends_at=h.now + timedelta(hours=1)
    )
    future = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    edge = await add_event(
        h, starts_at=h.now - timedelta(days=30, hours=2), ends_at=h.now - timedelta(days=30)
    )
    async with await h.client(member) as c:
        response = await c.put(
            f"/api/v1/events/{running.id}/attendance", json={"status": "attended"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "attended"
        assert await recompute_jobs(h, member) == 1
        response = await c.put(
            f"/api/v1/events/{future.id}/attendance", json={"status": "attended"}
        )
        assert_problem(response, 409, ErrorCode.ATTENDANCE_WINDOW_CLOSED)
        response = await c.put(f"/api/v1/events/{edge.id}/attendance", json={"status": "attended"})
        assert response.status_code == 200, response.text
    h.advance(timedelta(microseconds=1))
    async with await h.client(late) as c:
        response = await c.put(f"/api/v1/events/{edge.id}/attendance", json={"status": "attended"})
        assert_problem(response, 409, ErrorCode.ATTENDANCE_WINDOW_CLOSED)


async def test_body_limits_and_unknown_event(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    links = [f"https://example.com/proof/{index}" for index in range(6)]
    url = f"/api/v1/events/{event.id}/attendance"
    async with await h.client(member) as c:
        response = await c.put(url, json={"status": "going", "proof_links": links})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.put(url, json={"status": "going", "note": "x" * 1001})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.put(
            url, json={"status": "going", "proof_links": links[:5], "note": "See you there"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["proof_links"] == links[:5]
        assert response.json()["note"] == "See you there"
        response = await c.put(f"/api/v1/events/{uuid4()}/attendance", json={"status": "going"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    [row] = await attendance_rows(h, event)
    assert row.proof_links == links[:5]
    assert row.note == "See you there"


async def test_attended_cannot_go_back_to_going(harness: Harness) -> None:
    h = harness
    member = await h.user()
    event = await add_event(
        h, starts_at=h.now - timedelta(hours=1), ends_at=h.now + timedelta(hours=1)
    )
    url = f"/api/v1/events/{event.id}/attendance"
    async with await h.client(member) as c:
        response = await c.put(url, json={"status": "attended"})
        assert response.status_code == 200, response.text
        assert await recompute_jobs(h, member) == 1
        response = await c.put(url, json={"status": "going"})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        response = await c.put(url, json={"status": "attended"})
        assert response.status_code == 200, response.text
    assert await recompute_jobs(h, member) == 1
    [row] = await attendance_rows(h, event)
    assert row.status == "attended"


async def test_delete_attendance(harness: Harness) -> None:
    h = harness
    member = await h.user()
    stranger = await h.user()
    event = await ended_event(h)
    draft = await ended_event(h, status="draft")
    await attend(h, event, member, status_at=h.now - timedelta(hours=1), trust="peer")
    url = f"/api/v1/events/{event.id}/attendance"
    async with await h.client(member) as c:
        response = await c.delete(url)
        assert response.status_code == 204, response.text
        [row] = await attendance_rows(h, event)
        assert row.status == "cancelled"
        assert row.status_at == h.now
        assert row.trust == "peer"
        assert await recompute_jobs(h, member) == 1
        h.advance(timedelta(minutes=1))
        response = await c.delete(url)
        assert response.status_code == 204, response.text
        [row] = await attendance_rows(h, event)
        assert row.status_at == h.now - timedelta(minutes=1)
        assert await recompute_jobs(h, member) == 1
        response = await c.delete(f"/api/v1/events/{draft.id}/attendance")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(stranger) as c:
        response = await c.delete(url)
        assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_list_attendees(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    a, b, c_user, d = [await h.user() for _ in range(4)]
    event = await ended_event(h)
    draft = await ended_event(h, status="draft")
    row_a = await attend(h, event, a, "going", status_at=h.now - timedelta(hours=1))
    row_b = await attend(h, event, b, status_at=h.now - timedelta(hours=2))
    await attend(h, event, c_user, "cancelled", status_at=h.now)
    row_d = await attend(h, event, d, status_at=h.now - timedelta(days=3))
    url = f"/api/v1/events/{event.id}/attendees"
    async with await h.client(viewer) as c:
        response = await c.get(url)
        assert response.status_code == 200, response.text
        page = response.json()
        assert ids(page["items"]) == [str(row_a.id), str(row_b.id), str(row_d.id)]
        assert page["next_cursor"] is None
        assert page["items"][0]["user"]["id"] == str(a.id)
        response = await c.get(url, params={"status": "attended"})
        assert ids(response.json()["items"]) == [str(row_b.id), str(row_d.id)]
        response = await c.get(url, params={"status": "cancelled"})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        first = (await c.get(url, params={"limit": 1})).json()
        assert ids(first["items"]) == [str(row_a.id)]
        assert first["next_cursor"] is not None
        second = (await c.get(url, params={"cursor": first["next_cursor"]})).json()
        assert ids(second["items"]) == [str(row_b.id), str(row_d.id)]
        assert second["next_cursor"] is None
        response = await c.get(f"/api/v1/events/{draft.id}/attendees")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.get(url)
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)


async def test_attendees_confirm_action(harness: Harness) -> None:
    h = harness
    creator = await h.user()
    first = await h.user()
    second = await h.user()
    event = await ended_event(h, creator=creator)
    row_first = await attend(h, event, first, status_at=h.now - timedelta(hours=2))
    row_second = await attend(h, event, second, status_at=h.now - timedelta(hours=1))
    async with await h.client(second) as c:
        response = await c.get(f"/api/v1/events/{event.id}/attendees")
        assert response.status_code == 200, response.text
        actions = {item["id"]: item["allowed_actions"] for item in response.json()["items"]}
    assert actions[str(row_first.id)] == ["confirm"]
    assert actions[str(row_second.id)] == ["edit", "remove"]
    async with h.db() as session:
        await confirmations.add_confirmation(
            session,
            h.jobs,
            fact_type=ConfirmationFactType.ATTENDANCE,
            fact_id=row_first.id,
            subject_user_id=first.id,
            confirmer_id=creator.id,
            source=ConfirmationSource.ORGANIZER,
            now=h.now,
        )
    async with h.db() as session:
        row = await session.get(models.EventAttendance, row_first.id)
        assert row is not None
        assert row.trust == "peer"


async def test_list_my_attendances(harness: Harness) -> None:
    h = harness
    member = await h.user()
    other = await h.user()
    upcoming = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    past = await ended_event(h)
    dropped = await ended_event(h)
    going = await attend(h, upcoming, member, "going", status_at=h.now - timedelta(hours=1))
    attended = await attend(h, past, member, status_at=h.now - timedelta(hours=2))
    cancelled = await attend(h, dropped, member, "cancelled", status_at=h.now - timedelta(hours=3))
    await attend(h, past, other)
    async with await h.client(member) as c:
        response = await c.get("/api/v1/me/attendances")
        assert response.status_code == 200, response.text
        body = response.json()
        assert ids(body) == [str(going.id), str(attended.id), str(cancelled.id)]
        assert [item["event"]["id"] for item in body] == [
            str(upcoming.id),
            str(past.id),
            str(dropped.id),
        ]
        assert [item["allowed_actions"] for item in body] == [
            ["edit", "remove"],
            ["edit", "remove"],
            [],
        ]
        response = await c.get("/api/v1/me/attendances", params={"status": "attended"})
        assert ids(response.json()) == [str(attended.id)]


async def test_attended_sends_digest_once(harness: Harness) -> None:
    h = harness
    a = await h.user(display_name="Alpha")
    b = await h.user(display_name="Beta")
    event = await add_event(
        h, starts_at=h.now - timedelta(hours=3), ends_at=h.now - timedelta(hours=1)
    )
    row_b = await attend(h, event, b)
    await attend(h, event, a, "going", status_at=h.now - timedelta(hours=4))
    url = f"/api/v1/events/{event.id}/attendance"
    async with await h.client(a) as c:
        response = await c.put(url, json={"status": "attended"})
        assert response.status_code == 200, response.text
        row_a_id = UUID(response.json()["id"])
        async with h.db() as session:
            notifications = list(
                await session.scalars(
                    select(models.Notification).where(
                        models.Notification.type == "attendance_confirm_digest"
                    )
                )
            )
        payloads = {
            row.user_id: AttendanceConfirmDigestPayload.model_validate(row.payload)
            for row in notifications
        }
        assert len(notifications) == 2
        assert [item.attendance_id for item in payloads[a.id].attendees] == [row_b.id]
        assert [item.attendance_id for item in payloads[b.id].attendees] == [row_a_id]
        response = await c.put(url, json={"status": "attended"})
        assert response.status_code == 200, response.text
    async with h.db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.Notification)
            .where(models.Notification.type == "attendance_confirm_digest")
        )
    assert count == 2


async def test_onboarding_required(harness: Harness) -> None:
    h = harness
    member = await h.user(onboarded=False)
    event = await add_event(
        h, starts_at=h.now + timedelta(hours=1), ends_at=h.now + timedelta(hours=3)
    )
    async with await h.client(member) as c:
        responses = [
            await c.put(f"/api/v1/events/{event.id}/attendance", json={"status": "going"}),
            await c.delete(f"/api/v1/events/{event.id}/attendance"),
            await c.get(f"/api/v1/events/{event.id}/attendees"),
            await c.get("/api/v1/me/attendances"),
        ]
    for response in responses:
        assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)
    assert await attendance_rows(h, event) == []
