from datetime import date, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from harness import NOW, Harness
from hmm_contracts.common import ErrorCode
from hmm_core.models import ActivityEntry, Area, AuditLog, Notification, User
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


class FrozenDate(date):
    @classmethod
    def today(cls) -> Self:
        return cls(NOW.year, NOW.month, NOW.day)


@pytest.fixture(autouse=True)
def frozen_schema_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hmm_core.scores.schemas.date", FrozenDate)


def payload(h: Harness, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "oss",
        "title": "Shipped a parser",
        "description_md": "notes",
        "links": ["https://github.com/x/y"],
        "occurred_on": h.now.date().isoformat(),
    }
    body.update(fields)
    return body


async def count_rows(h: Harness) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(ActivityEntry)) or 0


async def load_entry(h: Harness, entry_id: str) -> ActivityEntry:
    async with h.db() as session:
        entry = await session.get(ActivityEntry, UUID(entry_id))
        assert entry is not None
        return entry


async def create(h: Harness, user: User, **fields: Any) -> dict[str, Any]:
    async with await h.client(user) as c:
        response = await c.post("/api/v1/me/activity", json=payload(h, **fields))
    assert response.status_code == 201, response.text
    return response.json()


async def test_create(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend"))
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post(
            "/api/v1/me/activity", json=payload(h, area_id=str(areas["backend"].id))
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["trust"] == "self"
    assert body["user_id"] == str(member.id)
    assert body["links"] == ["https://github.com/x/y"]
    assert body["allowed_actions"] == ["edit", "delete"]
    assert body["created_at"]
    assert body["updated_at"]
    async with h.db() as session:
        entry = (await session.scalars(select(ActivityEntry))).one()
        assert entry.type == "oss"
        assert entry.deleted_at is None
        assert entry.area_id == areas["backend"].id
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0
    jobs = await h.pending_jobs("recompute_member")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(member.id)}
    assert jobs[0]["lock"] == f"member:{member.id}"


async def test_date_boundaries(harness: Harness) -> None:
    h = harness
    member = await h.user()
    today = h.now.date()
    async with await h.client(member) as c:
        for value in (today, today - timedelta(days=365)):
            response = await c.post(
                "/api/v1/me/activity", json=payload(h, occurred_on=value.isoformat())
            )
            assert response.status_code == 201, response.text
        jobs = len(await h.pending_jobs("recompute_member"))
        for value in (today - timedelta(days=366), today + timedelta(days=1)):
            response = await c.post(
                "/api/v1/me/activity", json=payload(h, occurred_on=value.isoformat())
            )
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert await count_rows(h) == 2
        assert len(await h.pending_jobs("recompute_member")) == jobs
        created = await c.post(
            "/api/v1/me/activity",
            json=payload(h, occurred_on=(today - timedelta(days=10)).isoformat()),
        )
        entry_id = created.json()["id"]
        url = f"/api/v1/me/activity/{entry_id}"
        response = await c.patch(
            url, json={"occurred_on": (today - timedelta(days=365)).isoformat()}
        )
        assert response.status_code == 200, response.text
        assert response.json()["occurred_on"] == (today - timedelta(days=365)).isoformat()
        for value in (today - timedelta(days=366), today + timedelta(days=1)):
            response = await c.patch(url, json={"occurred_on": value.isoformat()})
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    entry = await load_entry(h, entry_id)
    assert entry.occurred_on == today - timedelta(days=365)


async def test_area_rules(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend"))
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.post("/api/v1/me/activity", json=payload(h, area_id=str(uuid4())))
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        assert await count_rows(h) == 0
        assert await h.pending_jobs("recompute_member") == []
        async with h.db() as session:
            frontend = await session.get(Area, areas["frontend"].id)
            assert frontend is not None
            frontend.archived_at = h.now
        response = await c.post(
            "/api/v1/me/activity", json=payload(h, area_id=str(areas["frontend"].id))
        )
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        assert await count_rows(h) == 0
        response = await c.post("/api/v1/me/activity", json=payload(h))
        assert response.status_code == 201, response.text
        assert response.json()["area_id"] is None
        url = f"/api/v1/me/activity/{response.json()['id']}"
        response = await c.patch(url, json={"area_id": str(areas["backend"].id)})
        assert response.status_code == 200, response.text
        assert response.json()["area_id"] == str(areas["backend"].id)
        response = await c.patch(url, json={"area_id": None})
        assert response.status_code == 200, response.text
        assert response.json()["area_id"] is None
        response = await c.patch(url, json={"area_id": str(areas["frontend"].id)})
        assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_input_bounds(harness: Harness) -> None:
    h = harness
    member = await h.user()
    invalid = [
        {"title": "x" * 201},
        {"description_md": "x" * 5001},
        {"links": [f"https://example.com/{index}" for index in range(11)]},
        {"title": "   "},
        {"type": "nope"},
        {"links": ["not a url"]},
    ]
    async with await h.client(member) as c:
        for fields in invalid:
            response = await c.post("/api/v1/me/activity", json=payload(h, **fields))
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        assert await count_rows(h) == 0
        assert await h.pending_jobs("recompute_member") == []
        response = await c.post("/api/v1/me/activity", json=payload(h, description_md="x" * 5000))
        assert response.status_code == 201, response.text


async def test_update_partial(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend"))
    member = await h.user()
    occurred = (h.now.date() - timedelta(days=3)).isoformat()
    created = await create(
        h,
        member,
        title="Original",
        description_md="body",
        area_id=str(areas["backend"].id),
        occurred_on=occurred,
    )
    assert len(await h.pending_jobs("recompute_member")) == 1
    url = f"/api/v1/me/activity/{created['id']}"
    async with await h.client(member) as c:
        response = await c.patch(url, json={"title": "Renamed"})
        assert response.status_code == 200, response.text
        renamed = response.json()
        assert renamed["title"] == "Renamed"
        assert renamed["description_md"] == "body"
        assert renamed["area_id"] == str(areas["backend"].id)
        assert renamed["occurred_on"] == occurred
        assert renamed["updated_at"]
        response = await c.patch(url, json={})
        assert response.status_code == 200, response.text
        assert response.content == (await c.patch(url, json={})).content
        assert response.json() == renamed
        response = await c.patch(url, json={"title": "Renamed"})
        assert response.status_code == 200, response.text
        assert response.json() == renamed
        response = await c.patch(url, json={"type": None})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.patch(url, json={"area_id": str(uuid4())})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    entry = await load_entry(h, created["id"])
    assert entry.title == "Renamed"
    assert entry.area_id == areas["backend"].id
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert len(await h.pending_jobs("recompute_member")) == 1


async def test_ownership_and_access(harness: Harness) -> None:
    h = harness
    member = await h.user()
    other = await h.user()
    admin = await h.admin()
    created = await create(h, member, title="Mine")
    url = f"/api/v1/me/activity/{created['id']}"
    async with await h.client(other) as c:
        assert_problem(await c.patch(url, json={"title": "Stolen"}), 403, ErrorCode.FORBIDDEN)
        assert_problem(await c.delete(url), 403, ErrorCode.FORBIDDEN)
        missing = f"/api/v1/me/activity/{uuid4()}"
        assert_problem(await c.patch(missing, json={"title": "x"}), 404, ErrorCode.NOT_FOUND)
        assert_problem(await c.delete(missing), 404, ErrorCode.NOT_FOUND)
        response = await c.post("/api/v1/me/activity", json=payload(h, area_id=str(uuid4())))
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(admin) as c:
        assert_problem(await c.patch(url, json={"title": "Admin"}), 403, ErrorCode.FORBIDDEN)
    entry = await load_entry(h, created["id"])
    assert entry.title == "Mine"
    assert entry.deleted_at is None
    async with h.anonymous() as c:
        response = await c.post("/api/v1/me/activity", json=payload(h))
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
        response = await c.get(f"/api/v1/users/{member.id}/activity")
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    pending = await h.user(onboarded=False)
    async with await h.client(pending) as c:
        response = await c.post("/api/v1/me/activity", json=payload(h))
        assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_soft_delete(harness: Harness) -> None:
    h = harness
    member = await h.user()
    created = await create(h, member)
    url = f"/api/v1/me/activity/{created['id']}"
    async with await h.client(member) as c:
        response = await c.delete(url)
        assert response.status_code == 204, response.text
        entry = await load_entry(h, created["id"])
        assert entry.deleted_at == h.now
        response = await c.get(f"/api/v1/users/{member.id}/activity")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        assert_problem(await c.patch(url, json={"title": "Back"}), 404, ErrorCode.NOT_FOUND)
        jobs = len(await h.pending_jobs("recompute_member"))
        response = await c.delete(url)
        assert response.status_code == 204, response.text
        assert len(await h.pending_jobs("recompute_member")) == jobs


async def test_list_ordering_and_pagination(harness: Harness) -> None:
    h = harness
    member = await h.user()
    other = await h.user()
    today = h.now.date()
    async with h.db() as session:
        entries = [
            ActivityEntry(
                user_id=member.id,
                type="oss",
                title=f"Entry {index}",
                description_md="",
                links=[],
                area_id=None,
                occurred_on=today - timedelta(days=index),
                created_at=h.now - timedelta(minutes=index),
                deleted_at=None,
            )
            for index in range(5)
        ]
        session.add_all(entries)
        await session.flush()
        for entry in entries:
            await session.refresh(entry)
        expected = [
            str(entry.id)
            for entry in sorted(
                entries,
                key=lambda entry: (entry.occurred_on, entry.created_at, entry.id),
                reverse=True,
            )
        ]
    url = f"/api/v1/users/{member.id}/activity"
    async with await h.client(member) as c:
        response = await c.get(url, params={"limit": 2})
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) == 2
        assert page["items"][0]["occurred_on"] == today.isoformat()
        assert page["next_cursor"]
        items = list(page["items"])
        for _ in range(2):
            response = await c.get(url, params={"limit": 2, "cursor": page["next_cursor"]})
            assert response.status_code == 200, response.text
            page = response.json()
            items.extend(page["items"])
        assert page["next_cursor"] is None
    assert [item["id"] for item in items] == expected
    assert len(set(expected)) == 5
    assert all(item["allowed_actions"] == ["edit", "delete"] for item in items)
    async with await h.client(other) as c:
        response = await c.get(url, params={"limit": 100})
        assert response.status_code == 200, response.text
        seen = response.json()["items"]
        assert [item["id"] for item in seen] == expected
        assert all(item["allowed_actions"] == [] for item in seen)
        response = await c.get(f"/api/v1/users/{uuid4()}/activity")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        pending = await h.user(onboarded=False)
        response = await c.get(f"/api/v1/users/{pending.id}/activity")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
