from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_contracts.notifications import EventCancelledPayload, EventChangedPayload
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

URL = "/api/v1/events"


def event_body(h: Harness, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "meetup",
        "title": "Harbor Build Weekend",
        "starts_at": (h.now + timedelta(days=30)).isoformat(),
        "ends_at": (h.now + timedelta(days=31)).isoformat(),
        "format": "offline",
        "area_keys": ["backend"],
    }
    body.update(fields)
    return body


async def add_event(
    h: Harness,
    *,
    title: str = "Existing event",
    starts_at: datetime,
    ends_at: datetime,
    status: str = "published",
    website_url: str | None = None,
    origin: str = "manual",
) -> models.Event:
    async with h.db() as session:
        row = models.Event(
            slug=f"existing-{uuid4()}",
            kind="meetup",
            formation_enabled=False,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone="UTC",
            website_url=website_url,
            language=[],
            themes=[],
            status=status,
            review_reasons=[],
            origin=origin,
            unmapped_areas=[],
        )
        session.add(row)
        await session.flush()
        return row


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def test_create_member_event(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post(URL, json=event_body(h))
        assert response.status_code == 201, response.text
        body = response.json()
        hackathon = await c.post(
            URL,
            json=event_body(
                h,
                kind="hackathon",
                title="Totally different hackathon",
                starts_at=(h.now + timedelta(days=90)).isoformat(),
                ends_at=(h.now + timedelta(days=91)).isoformat(),
            ),
        )
        assert hackathon.status_code == 201, hackathon.text
    assert body["origin"] == "member"
    assert body["status"] == "published"
    assert body["created_by"] == str(member.id)
    assert body["formation_enabled"] is False
    assert body["slug"]
    assert body["area_keys"] == ["backend"]
    assert body["timezone"] == "UTC"
    assert body["format"] == "offline"
    assert body["review_reasons"] == []
    assert body["allowed_actions"] == ["edit", "cancel"]
    assert hackathon.json()["formation_enabled"] is False
    event_id = UUID(body["id"])
    async with h.db() as session:
        [attendance] = list(
            await session.scalars(
                select(models.EventAttendance).where(models.EventAttendance.event_id == event_id)
            )
        )
        assert attendance.user_id == member.id
        assert attendance.status == "going"
        assert attendance.role == "organizer"
        assert attendance.trust == "self"
        assert attendance.status_at == h.now
        kinds = sorted(
            await session.scalars(
                select(models.FeedItem.kind).where(models.FeedItem.subject_id == event_id)
            )
        )
        assert kinds == ["event_going", "event_new"]
        [audit] = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.entity_id == str(event_id))
            )
        )
        assert audit.action == "event_create"
        assert audit.actor_kind == "user"
        assert audit.actor_id == member.id
    assert await h.pending_jobs("compute_lead_fits") == []
    assert await count(h, models.EventFormation) == 0


async def test_past_member_event_marks_creator_attended(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post(
            URL,
            json=event_body(
                h,
                starts_at=(h.now - timedelta(hours=2)).isoformat(),
                ends_at=(h.now - timedelta(hours=1)).isoformat(),
            ),
        )
        assert response.status_code == 201, response.text
    event_id = UUID(response.json()["id"])
    async with h.db() as session:
        attendance = await session.scalar(
            select(models.EventAttendance).where(models.EventAttendance.event_id == event_id)
        )
        assert attendance is not None
        assert attendance.status == "attended"
    jobs = await h.pending_jobs("recompute_member")
    assert [job["args"] for job in jobs] == [{"user_id": str(member.id)}]
    assert await count(h, models.FeedItem, models.FeedItem.subject_id == event_id) == 0


async def test_duplicate_website_url(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    existing = await add_event(
        h,
        starts_at=h.now + timedelta(days=100),
        ends_at=h.now + timedelta(days=101),
        website_url="https://harbor.example/build",
    )
    body = event_body(h, website_url="https://harbor.example/build")
    async with await h.client(member) as c:
        response = await c.post(URL, json=body)
        problem = assert_problem(response, 409, ErrorCode.POSSIBLE_DUPLICATE)
        assert [issue.loc[0] for issue in problem.errors] == [str(existing.id)]
        assert problem.errors[0].msg == existing.title
        assert await count(h, models.Event) == 1
        assert await count(h, models.EventAttendance) == 0
        response = await c.post(URL, params={"confirm_new": "true"}, json=body)
        assert response.status_code == 201, response.text
        assert response.json()["review_reasons"] == ["possible_duplicate"]


async def test_confirm_new_without_candidates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post(URL, params={"confirm_new": "true"}, json=event_body(h))
        assert response.status_code == 201, response.text
        assert response.json()["review_reasons"] == []


async def test_duplicate_title_rule(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    await add_event(
        h,
        title="Harbor Build Weekend 2026!",
        starts_at=h.now + timedelta(days=30),
        ends_at=h.now + timedelta(days=31),
    )
    for status in ("archived", "cancelled"):
        await add_event(
            h,
            title="Harbor Build Weekend",
            starts_at=h.now + timedelta(days=60),
            ends_at=h.now + timedelta(days=61),
            status=status,
        )
    async with await h.client(member) as c:
        response = await c.post(
            URL,
            json=event_body(
                h,
                title="harbor-build-weekend",
                starts_at=(h.now + timedelta(days=32)).isoformat(),
                ends_at=(h.now + timedelta(days=33)).isoformat(),
            ),
        )
        assert_problem(response, 409, ErrorCode.POSSIBLE_DUPLICATE)
        response = await c.post(
            URL,
            json=event_body(
                h,
                title="harbor-build-weekend",
                starts_at=(h.now + timedelta(days=32, seconds=1)).isoformat(),
                ends_at=(h.now + timedelta(days=33, seconds=1)).isoformat(),
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["review_reasons"] == []
        response = await c.post(
            URL,
            json=event_body(
                h,
                starts_at=(h.now + timedelta(days=60)).isoformat(),
                ends_at=(h.now + timedelta(days=61)).isoformat(),
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["review_reasons"] == []


async def test_create_member_event_validation(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post(
            URL,
            json=event_body(
                h,
                starts_at=(h.now + timedelta(days=31)).isoformat(),
                ends_at=(h.now + timedelta(days=30)).isoformat(),
            ),
        )
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.post(URL, json=event_body(h, area_keys=["nonexistent"]))
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.post(URL, json=event_body(h, area_keys=["backend", "backend"]))
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    assert await count(h, models.Event) == 0


async def test_creating_events_restriction(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    member = await h.user()
    async with h.db() as session:
        restriction = models.MemberRestriction(
            user_id=member.id, kind="creating_events", reason="spam", created_by=admin.id
        )
        session.add(restriction)
        await session.flush()
        restriction_id = restriction.id
    async with await h.client(member) as c:
        response = await c.post(URL, json=event_body(h))
        assert_problem(response, 403, ErrorCode.RESTRICTED)
        assert await count(h, models.Event) == 0
        async with h.db() as session:
            row = await session.get(models.MemberRestriction, restriction_id)
            assert row is not None
            row.lifted_at = h.now
        response = await c.post(URL, json=event_body(h))
        assert response.status_code == 201, response.text


async def test_create_member_event_auth(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user(onboarded=False)
    async with await h.client(member) as c:
        response = await c.post(URL, json=event_body(h))
        assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)
    async with h.anonymous() as c:
        response = await c.post(URL, json=event_body(h))
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    assert await count(h, models.Event) == 0


async def create(h: Harness, user: models.User, **fields: Any) -> dict[str, Any]:
    async with await h.client(user) as c:
        response = await c.post(URL, params={"confirm_new": "true"}, json=event_body(h, **fields))
        assert response.status_code == 201, response.text
        return response.json()


async def notifications(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification)
                .where(models.Notification.type == kind)
                .order_by(models.Notification.id)
            )
        )


async def audits(h: Harness, action: str, event_id: str) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == action, models.AuditLog.entity_id == event_id
                )
            )
        )


async def seed_attendance(
    h: Harness, event_id: str, user: models.User, status: str
) -> models.EventAttendance:
    async with h.db() as session:
        row = models.EventAttendance(
            event_id=UUID(event_id),
            user_id=user.id,
            status=status,
            role="attendee",
            proof_links=[],
            trust="self",
            status_at=h.now - timedelta(hours=1),
        )
        session.add(row)
        await session.flush()
        return row


async def test_update_member_event_title_revision_and_audit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    event = await create(h, creator)
    async with await h.client(creator) as c:
        response = await c.patch(f"{URL}/{event['id']}", json={"title": "New title"})
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "New title"
        assert response.json()["allowed_actions"] == ["edit", "cancel"]
        response = await c.patch(f"{URL}/{event['id']}", json={})
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "New title"
    async with h.db() as session:
        [revision] = list(
            await session.scalars(
                select(models.EventRevision).where(
                    models.EventRevision.event_id == UUID(event["id"])
                )
            )
        )
    assert revision.origin == "member"
    assert revision.actor_id == creator.id
    assert revision.diff == {"title": {"before": event["title"], "after": "New title"}}
    [audit] = await audits(h, "event_edit", event["id"])
    assert audit.actor_kind == "user"
    assert audit.before == {"title": event["title"]}
    assert audit.after == {"title": "New title"}
    assert await notifications(h, "event_changed") == []


async def test_update_member_event_notifies_on_date_and_format_changes(
    harness: Harness,
) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    attendee = await h.user()
    event = await create(h, creator)
    url = f"{URL}/{event['id']}"
    async with await h.client(attendee) as c:
        response = await c.put(f"{url}/attendance", json={"status": "going"})
        assert response.status_code == 200, response.text
    async with await h.client(creator) as c:
        response = await c.patch(
            url,
            json={
                "starts_at": (h.now + timedelta(days=40)).isoformat(),
                "ends_at": (h.now + timedelta(days=41)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        rows = await notifications(h, "event_changed")
        assert sorted(row.user_id for row in rows) == sorted([creator.id, attendee.id])
        for row in rows:
            payload = EventChangedPayload.model_validate(row.payload)
            assert payload.changes[0].field in {"starts_at", "ends_at"}
            assert [change.field for change in payload.changes] == ["ends_at", "starts_at"]
        response = await c.patch(url, json={"title": "Renamed"})
        assert response.status_code == 200, response.text
        assert len(await notifications(h, "event_changed")) == 2
        h.advance(timedelta(minutes=1))
        response = await c.patch(url, json={"format": "online"})
        assert response.status_code == 200, response.text
        assert response.json()["format"] == "online"
        rows = await notifications(h, "event_changed")
        assert len(rows) == 4
        assert [
            change.field for change in EventChangedPayload.model_validate(rows[-1].payload).changes
        ] == ["format"]
        response = await c.patch(url, json={"starts_at": (h.now + timedelta(days=50)).isoformat()})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)


async def test_update_member_event_areas(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    event = await create(h, creator)
    url = f"{URL}/{event['id']}"
    event_id = UUID(event["id"])
    async with await h.client(creator) as c:
        response = await c.patch(url, json={"area_keys": ["design"]})
        assert response.status_code == 200, response.text
        assert response.json()["area_keys"] == ["design"]
        assert await count(h, models.EventArea, models.EventArea.event_id == event_id) == 1
        response = await c.patch(url, json={"area_keys": ["nope"]})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.patch(url, json={"area_keys": None})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        async with h.db() as session:
            keys = list(
                await session.scalars(
                    select(models.Area.key)
                    .join(models.EventArea, models.EventArea.area_id == models.Area.id)
                    .where(models.EventArea.event_id == event_id)
                )
            )
        assert keys == ["design"]
        response = await c.patch(url, json={"area_keys": []})
        assert response.status_code == 200, response.text
        assert response.json()["area_keys"] == []
    assert await count(h, models.EventArea, models.EventArea.event_id == event_id) == 0


async def test_update_member_event_permissions(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    other = await h.user()
    admin = await h.admin()
    event = await create(
        h,
        creator,
        starts_at=(h.now + timedelta(hours=1)).isoformat(),
        ends_at=(h.now + timedelta(hours=3)).isoformat(),
    )
    imported = await add_event(
        h,
        starts_at=h.now + timedelta(days=5),
        ends_at=h.now + timedelta(days=6),
        origin="import",
    )
    url = f"{URL}/{event['id']}"
    async with await h.client(other) as c:
        response = await c.patch(url, json={"title": "Hijack"})
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    h.advance(timedelta(hours=2))
    async with await h.client(creator) as c:
        response = await c.patch(url, json={"title": "Too late"})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        response = await c.patch(f"{URL}/{uuid4()}", json={"title": "Missing"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(admin) as c:
        response = await c.patch(url, json={"title": "Admin fix"})
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Admin fix"
        assert response.json()["allowed_actions"] == ["edit", "cancel"]
        response = await c.patch(f"{URL}/{imported.id}", json={"title": "Imported"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    [audit] = await audits(h, "event_edit", event["id"])
    assert audit.actor_kind == "admin"


async def test_cancel_member_event(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    going = await h.user()
    attended = await h.user()
    event = await create(h, creator)
    url = f"{URL}/{event['id']}"
    event_id = UUID(event["id"])
    async with await h.client(going) as c:
        response = await c.put(f"{url}/attendance", json={"status": "going"})
        assert response.status_code == 200, response.text
    await seed_attendance(h, event["id"], attended, "attended")
    h.advance(timedelta(minutes=5))
    async with await h.client(creator) as c:
        response = await c.post(f"{url}/cancel", json={"reason": "Venue closed"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["allowed_actions"] == ["edit"]
        cancelled_at = h.now
        async with h.db() as session:
            rows = {
                row.user_id: row
                for row in await session.scalars(
                    select(models.EventAttendance).where(
                        models.EventAttendance.event_id == event_id
                    )
                )
            }
            hidden = list(
                await session.scalars(
                    select(models.FeedItem.hidden_at).where(models.FeedItem.subject_id == event_id)
                )
            )
        assert rows[going.id].status == "cancelled"
        assert rows[going.id].status_at == cancelled_at
        assert rows[creator.id].status == "cancelled"
        assert rows[attended.id].status == "attended"
        assert hidden and all(value == cancelled_at for value in hidden)
        notices = await notifications(h, "event_cancelled")
        assert sorted(row.user_id for row in notices) == sorted([creator.id, going.id, attended.id])
        assert {EventCancelledPayload.model_validate(row.payload).reason for row in notices} == {
            "Venue closed"
        }
        [audit] = await audits(h, "event_cancel", event["id"])
        assert audit.actor_kind == "user"
        h.advance(timedelta(minutes=1))
        response = await c.post(f"{url}/cancel", json={"reason": "Venue closed"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert len(await audits(h, "event_cancel", event["id"])) == 1
        assert len(await notifications(h, "event_cancelled")) == 3
        async with h.db() as session:
            hidden = list(
                await session.scalars(
                    select(models.FeedItem.hidden_at).where(models.FeedItem.subject_id == event_id)
                )
            )
        assert all(value == cancelled_at for value in hidden)
        response = await c.patch(url, json={"title": "Cancelled but renamed"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["title"] == "Cancelled but renamed"


async def test_cancel_member_event_permissions(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    other = await h.user()
    admin = await h.admin()
    event = await create(h, creator)
    archived = await create(
        h,
        creator,
        title="Archived meetup",
        starts_at=(h.now + timedelta(days=70)).isoformat(),
        ends_at=(h.now + timedelta(days=71)).isoformat(),
    )
    async with h.db() as session:
        row = await session.get(models.Event, UUID(archived["id"]))
        assert row is not None
        row.status = "archived"
    async with await h.client(other) as c:
        response = await c.post(f"{URL}/{event['id']}/cancel", json={"reason": "Nope"})
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with await h.client(admin) as c:
        response = await c.post(f"{URL}/{event['id']}/cancel", json={"reason": "Duplicate"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        response = await c.post(f"{URL}/{archived['id']}/cancel", json={"reason": "Old"})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    async with await h.client(creator) as c:
        response = await c.post(f"{URL}/{archived['id']}/cancel", json={"reason": "Old"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    [audit] = await audits(h, "event_cancel", event["id"])
    assert audit.actor_kind == "admin"


async def test_cancel_past_member_event_notifies_attended(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    creator = await h.user()
    attendee = await h.user()
    event = await create(
        h,
        creator,
        starts_at=(h.now - timedelta(hours=2)).isoformat(),
        ends_at=(h.now - timedelta(hours=1)).isoformat(),
    )
    await seed_attendance(h, event["id"], attendee, "attended")
    async with await h.client(creator) as c:
        response = await c.post(f"{URL}/{event['id']}/cancel", json={"reason": "Never happened"})
        assert response.status_code == 200, response.text
        assert response.json()["allowed_actions"] == []
    notices = await notifications(h, "event_cancelled")
    assert sorted(row.user_id for row in notices) == sorted([creator.id, attendee.id])
