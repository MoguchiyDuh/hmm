from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from hmm_core.mentorship.service import expire_help_requests
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

URL = "/api/v1/help-requests"


def body(area_key: str = "ml_ai", **fields: Any) -> dict[str, Any]:
    return {
        "area_key": area_key,
        "topic": "FastAPI auth",
        "goal": "ship a login flow",
        "format": "online",
        **fields,
    }


async def create(c: httpx.AsyncClient, area_key: str = "ml_ai", **fields: Any) -> dict[str, Any]:
    response = await c.post(URL, json=body(area_key, **fields))
    assert response.status_code == 201, response.text
    return response.json()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def add_offers(h: Harness, request_id: str, *mentors: models.User) -> list[UUID]:
    async with h.db() as session:
        offers = [
            models.HelpOffer(
                request_id=UUID(request_id),
                mentor_id=mentor.id,
                message=f"I can help {mentor.display_name}",
                status="pending",
            )
            for mentor in mentors
        ]
        session.add_all(offers)
        await session.flush()
        return [offer.id for offer in offers]


async def notifications(h: Harness, user_id: UUID, type: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user_id, models.Notification.type == type
                )
            )
        )


async def feed_items(h: Harness, request_id: str, *, live: bool) -> int:
    async with h.db() as session:
        statement = (
            select(func.count())
            .select_from(models.FeedItem)
            .where(
                models.FeedItem.kind == "help_request",
                models.FeedItem.subject_id == UUID(request_id),
            )
        )
        if live:
            statement = statement.where(models.FeedItem.hidden_at.is_(None))
        else:
            statement = statement.where(models.FeedItem.hidden_at.is_not(None))
        return await session.scalar(statement) or 0


async def set_status(h: Harness, request_id: str, status: str) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.HelpRequest)
            .where(models.HelpRequest.id == UUID(request_id))
            .values(status=status)
        )


async def add_active_mentorship(
    h: Harness, learner: models.User, mentor: models.User, area: models.Area
) -> None:
    async with h.db() as session:
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=area.id,
            topic="Past",
            goal="Past goal",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=14),
        )
        session.add(request)
        await session.flush()
        session.add(
            models.Mentorship(
                request_id=request.id,
                mentor_id=mentor.id,
                learner_id=learner.id,
                area_id=area.id,
                kind="mentoring",
                mentor_score_at_start=Decimal("7.00"),
                learner_score_at_start=Decimal("2.00"),
                status="active",
                started_at=h.now,
                mentor_trust="self",
                learner_trust="self",
            )
        )


async def test_create_open_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    async with await h.client(learner) as c:
        created = await create(c)
    assert created["status"] == "open"
    assert created["target_mentor_id"] is None
    assert created["offers"] == []
    assert created["allowed_actions"] == ["edit", "cancel"]
    assert created["learner_id"] == str(learner.id)
    assert created["area_key"] == "ml_ai"
    assert parse_time(created["expires_at"]) == h.now + timedelta(days=14)
    assert await feed_items(h, created["id"], live=True) == 1
    jobs = await h.pending_jobs("match_mentors")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"request_id": created["id"]}
    assert jobs[0]["lock"] == f"mentor:{created['id']}"


async def test_request_limits_and_lazy_expiry(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        first = await create(c)
        assert_problem(await c.post(URL, json=body()), 409, ErrorCode.HELP_REQUEST_OPEN)
        assert_problem(
            await c.post(URL, json=body(target_mentor_id=str(mentor.id))),
            409,
            ErrorCode.HELP_REQUEST_OPEN,
        )
        await create(c, "backend")
        offer_ids = await add_offers(h, first["id"], mentor, other)
        h.advance(timedelta(days=15))
        renewed = await create(c)
        assert renewed["id"] != first["id"]
        expired = await c.get(f"{URL}/{first['id']}")
        assert expired.status_code == 200, expired.text
        assert expired.json()["status"] == "expired"
        assert expired.json()["allowed_actions"] == []
    assert await feed_items(h, first["id"], live=True) == 0
    assert await feed_items(h, first["id"], live=False) == 1
    async with h.db() as session:
        offers = list(
            await session.scalars(
                select(models.HelpOffer).where(models.HelpOffer.id.in_(offer_ids))
            )
        )
        assert {offer.status for offer in offers} == {"closed"}
    for author in (mentor, other):
        closed = await notifications(h, author.id, "help_offer_closed")
        assert len(closed) == 1
        assert isinstance(closed[0].payload, dict)
        assert closed[0].payload["reason"] == "The help request expired."
    expiries = await notifications(h, learner.id, "help_request_expired")
    assert len(expiries) == 1
    assert isinstance(expiries[0].payload, dict)
    assert expiries[0].payload["actions"] == [
        {
            "kind": "link",
            "label_key": "notifications.actions.reopen",
            "url": f"/mentorship/new?from={first['id']}",
        }
    ]

    await add_active_mentorship(h, learner, mentor, areas["design"])
    await add_active_mentorship(h, learner, other, areas["qa"])
    async with await h.client(learner) as c:
        assert_problem(await c.post(URL, json=body("data")), 409, ErrorCode.MENTEE_LIMIT_REACHED)


async def test_expire_help_requests_batch(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        created = await create(c)
    offer_ids = await add_offers(h, created["id"], mentor, other)
    h.advance(timedelta(days=15))
    async with h.db() as session:
        assert await expire_help_requests(session, h.now) == 1
    async with h.db() as session:
        row = await session.get(models.HelpRequest, UUID(created["id"]))
        assert row is not None
        assert row.status == "expired"
        offers = list(
            await session.scalars(
                select(models.HelpOffer).where(models.HelpOffer.id.in_(offer_ids))
            )
        )
        assert {offer.status for offer in offers} == {"closed"}
    for author in (mentor, other):
        assert len(await notifications(h, author.id, "help_offer_closed")) == 1
    assert len(await notifications(h, learner.id, "help_request_expired")) == 1
    assert await feed_items(h, created["id"], live=True) == 0
    async with h.db() as session:
        assert await expire_help_requests(session, h.now) == 0
    async with await h.client(other) as c:
        assert_problem(await c.get(f"{URL}/{created['id']}"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        assert_problem(
            await c.patch(f"{URL}/{created['id']}", json={"topic": "Late"}),
            409,
            ErrorCode.INVALID_STATE,
        )


async def test_direct_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        created = await create(c, target_mentor_id=str(mentor.id))
    assert created["status"] == "direct"
    assert created["target_mentor_id"] == str(mentor.id)
    assert created["allowed_actions"] == ["edit", "cancel"]
    assert await feed_items(h, created["id"], live=True) == 0
    assert await h.pending_jobs("match_mentors") == []
    direct = await notifications(h, mentor.id, "help_request_direct")
    assert len(direct) == 1
    payload = direct[0].payload
    assert isinstance(payload, dict)
    assert isinstance(payload["respond_by"], str)
    assert parse_time(payload["respond_by"]) == h.now + timedelta(days=3)
    assert payload["link"] == f"/help-requests/{created['id']}"
    assert payload["learner"] == {"user_id": str(learner.id), "display_name": "Learner"}
    assert payload["actions"] == [
        {
            "kind": "api",
            "key": "help_request_decline",
            "entity_id": created["id"],
            "label_key": "notifications.actions.help_request_decline",
        },
        {"kind": "link", "label_key": "notifications.actions.help", "url": "/mentorship"},
    ]
    assert await notifications(h, other.id, "help_request_direct") == []


async def test_direct_request_errors(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    busy = await h.user(login="busy", dnd_until=h.now + timedelta(days=1))
    newcomer = await h.user(login="newcomer", onboarded=False)
    async with await h.client(learner) as c:
        assert_problem(
            await c.post(URL, json=body(target_mentor_id=str(busy.id))), 409, ErrorCode.USER_DND
        )
        assert_problem(
            await c.post(URL, json=body(target_mentor_id=str(uuid4()))), 404, ErrorCode.NOT_FOUND
        )
        assert_problem(
            await c.post(URL, json=body(target_mentor_id=str(newcomer.id))),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await c.post(URL, json=body(target_mentor_id=str(learner.id))),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(await c.post(URL, json=body("nope")), 404, ErrorCode.NOT_FOUND)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.HelpRequest)) == 0


async def test_request_visibility(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        direct = await create(c, "backend", target_mentor_id=str(mentor.id))
        opened = await create(c)
        await add_offers(h, opened["id"], mentor)
        own = await c.get(f"{URL}/{opened['id']}")
        assert own.status_code == 200, own.text
        assert [offer["mentor_id"] for offer in own.json()["offers"]] == [str(mentor.id)]
        own_direct = await c.get(f"{URL}/{direct['id']}")
        assert own_direct.status_code == 200
        assert own_direct.json()["offers"] == []
    async with await h.client(mentor) as c:
        target_view = await c.get(f"{URL}/{direct['id']}")
        assert target_view.status_code == 200, target_view.text
        assert target_view.json()["offers"] == []
        assert target_view.json()["allowed_actions"] == ["decline", "offer_help"]
        mentor_view = await c.get(f"{URL}/{opened['id']}")
        assert mentor_view.status_code == 200
        assert mentor_view.json()["offers"] == []
    async with await h.client(other) as c:
        assert_problem(await c.get(f"{URL}/{direct['id']}"), 404, ErrorCode.NOT_FOUND)
        public = await c.get(f"{URL}/{opened['id']}")
        assert public.status_code == 200, public.text
        assert public.json()["offers"] == []
        assert public.json()["allowed_actions"] == ["offer_help"]
    await set_status(h, opened["id"], "cancelled")
    async with await h.client(other) as c:
        assert_problem(await c.get(f"{URL}/{opened['id']}"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        cancelled = await c.get(f"{URL}/{opened['id']}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
    async with await h.client(learner) as c:
        assert_problem(await c.get(f"{URL}/{uuid4()}"), 404, ErrorCode.NOT_FOUND)


async def test_list_requests(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        ml = await create(c)
        direct = await create(c, "backend", target_mentor_id=str(mentor.id))
    async with await h.client(other) as c:
        data = await create(c, "data")
        listed = await c.get(URL)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [data["id"], ml["id"]]
        assert listed.json()["next_cursor"] is None
        hidden = await c.get(URL, params={"status": "direct"})
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        by_area = await c.get(URL, params={"area": "ml_ai"})
        assert [item["id"] for item in by_area.json()["items"]] == [ml["id"]]
        assert_problem(await c.get(URL, params={"area": "nope"}), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        own_direct = await c.get(URL, params={"status": "direct"})
        assert [item["id"] for item in own_direct.json()["items"]] == [direct["id"]]
    async with await h.client(mentor) as c:
        targeted = await c.get(URL, params={"status": "direct"})
        assert [item["id"] for item in targeted.json()["items"]] == [direct["id"]]
        third = await create(c, "design")
        first_page = await c.get(URL, params={"limit": 2})
        assert first_page.status_code == 200, first_page.text
        page = first_page.json()
        assert [item["id"] for item in page["items"]] == [third["id"], data["id"]]
        assert page["next_cursor"] is not None
        second_page = await c.get(URL, params={"limit": 2, "cursor": page["next_cursor"]})
        assert second_page.status_code == 200, second_page.text
        assert [item["id"] for item in second_page.json()["items"]] == [ml["id"]]
        assert second_page.json()["next_cursor"] is None


async def test_update_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        created = await create(c)
        response = await c.patch(
            f"{URL}/{created['id']}", json={"topic": "Renamed", "format": "offline"}
        )
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["topic"] == "Renamed"
        assert updated["format"] == "offline"
        assert updated["goal"] == "ship a login flow"
        unchanged = await c.patch(f"{URL}/{created['id']}", json={})
        assert unchanged.status_code == 200, unchanged.text
        assert {key: unchanged.json()[key] for key in ("topic", "goal", "format")} == {
            "topic": "Renamed",
            "goal": "ship a login flow",
            "format": "offline",
        }
        assert_problem(
            await c.patch(f"{URL}/{uuid4()}", json={"topic": "Nope"}), 404, ErrorCode.NOT_FOUND
        )
    async with await h.client(other) as c:
        assert_problem(
            await c.patch(f"{URL}/{created['id']}", json={"topic": "Hijack"}),
            403,
            ErrorCode.FORBIDDEN,
        )
    await set_status(h, created["id"], "cancelled")
    async with await h.client(learner) as c:
        assert_problem(
            await c.patch(f"{URL}/{created['id']}", json={"topic": "Again"}),
            409,
            ErrorCode.INVALID_STATE,
        )


async def test_help_requests_auth(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    async with h.anonymous() as c:
        assert_problem(await c.get(URL), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await c.post(URL, json=body()), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await c.get(f"{URL}/{uuid4()}"), 401, ErrorCode.UNAUTHENTICATED)
    newcomer = await h.user(login="newcomer", onboarded=False)
    async with await h.client(newcomer) as c:
        assert_problem(await c.get(URL), 403, ErrorCode.ONBOARDING_REQUIRED)
        assert_problem(await c.post(URL, json=body()), 403, ErrorCode.ONBOARDING_REQUIRED)
        assert_problem(
            await c.patch(f"{URL}/{uuid4()}", json={"topic": "x"}),
            403,
            ErrorCode.ONBOARDING_REQUIRED,
        )
