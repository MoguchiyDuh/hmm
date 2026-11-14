from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ErrorCode
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
            origin="manual",
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
