from datetime import timedelta
from typing import Any
from uuid import UUID

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


async def write_request(h: Harness, request_id: str, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.HelpRequest)
            .where(models.HelpRequest.id == UUID(request_id))
            .values(**values)
        )


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


async def offer_statuses(h: Harness, offer_ids: list[UUID]) -> set[str]:
    async with h.db() as session:
        return set(
            await session.scalars(
                select(models.HelpOffer.status).where(models.HelpOffer.id.in_(offer_ids))
            )
        )


async def notifications(
    h: Harness, type: str, user_id: UUID | None = None
) -> list[models.Notification]:
    async with h.db() as session:
        statement = select(models.Notification).where(models.Notification.type == type)
        if user_id is not None:
            statement = statement.where(models.Notification.user_id == user_id)
        return list(await session.scalars(statement))


async def notification_count(h: Harness) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(models.Notification)) or 0


async def feed_items(h: Harness, request_id: str, *, live: bool) -> int:
    async with h.db() as session:
        hidden = (
            models.FeedItem.hidden_at.is_(None) if live else models.FeedItem.hidden_at.is_not(None)
        )
        return (
            await session.scalar(
                select(func.count())
                .select_from(models.FeedItem)
                .where(
                    models.FeedItem.kind == "help_request",
                    models.FeedItem.subject_id == UUID(request_id),
                    hidden,
                )
            )
            or 0
        )


async def test_cancel_open_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        opened = await create(c)
        direct = await create(c, "backend", target_mentor_id=str(target.id))
    before = await notification_count(h)
    async with await h.client(target) as c:
        assert_problem(await c.post(f"{URL}/{opened['id']}/cancel"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(other) as c:
        assert_problem(await c.post(f"{URL}/{direct['id']}/cancel"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        response = await c.post(f"{URL}/{opened['id']}/cancel")
        assert response.status_code == 200, response.text
        cancelled = response.json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["allowed_actions"] == []
        again = await c.post(f"{URL}/{opened['id']}/cancel")
        assert again.status_code == 200, again.text
        assert again.json() == cancelled
    assert await feed_items(h, opened["id"], live=True) == 0
    assert await feed_items(h, opened["id"], live=False) == 1
    assert await notification_count(h) == before


async def test_cancel_closes_pending_offers(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        opened = await create(c)
        offer_ids = await add_offers(h, opened["id"], mentor, other)
        response = await c.post(f"{URL}/{opened['id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["offers"][0]["allowed_actions"] == []
    assert await offer_statuses(h, offer_ids) == {"closed"}
    for author, offer_id in zip((mentor, other), offer_ids, strict=True):
        closed = await notifications(h, "help_offer_closed", author.id)
        assert len(closed) == 1
        payload = closed[0].payload
        assert isinstance(payload, dict)
        assert payload["reason"] == "The learner cancelled the help request."
        assert payload["link"] == f"/help-requests/{opened['id']}"
        assert payload["help_offer_id"] == str(offer_id)
        assert payload["actions"] == []


async def test_decline_direct_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        direct = await create(c, target_mentor_id=str(target.id))
    decline = f"{URL}/{direct['id']}/decline"
    async with await h.client(other) as c:
        assert_problem(await c.post(decline), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        assert_problem(await c.post(decline), 403, ErrorCode.FORBIDDEN)
    async with await h.client(target) as c:
        response = await c.post(decline)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "open"
        assert response.json()["allowed_actions"] == []
        assert_problem(await c.post(decline), 409, ErrorCode.INVALID_STATE)
    assert await feed_items(h, direct["id"], live=True) == 1
    jobs = await h.pending_jobs("match_mentors")
    assert [job["args"] for job in jobs] == [{"request_id": direct["id"]}]
    declined = await notifications(h, "help_request_declined")
    assert [row.user_id for row in declined] == [learner.id]
    payload = declined[0].payload
    assert isinstance(payload, dict)
    assert payload["link"] == "/mentorship"
    assert payload["actions"] == [
        {
            "kind": "api",
            "key": "help_request_open",
            "entity_id": direct["id"],
            "label_key": "notifications.actions.help_request_open",
        }
    ]
    async with await h.client(learner) as c:
        reopened = await c.post(f"{URL}/{direct['id']}/open")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "open"
        assert reopened.json()["allowed_actions"] == ["edit", "cancel"]
    async with await h.client(other) as c:
        visible = await c.get(f"{URL}/{direct['id']}")
        assert visible.status_code == 200, visible.text
        assert visible.json()["allowed_actions"] == ["offer_help"]
    assert len(await h.pending_jobs("match_mentors")) == 1
    assert await feed_items(h, direct["id"], live=True) == 1


async def test_open_after_three_days(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    async with await h.client(learner) as c:
        direct = await create(c, target_mentor_id=str(target.id))
        await write_request(h, direct["id"], created_at=h.now)
        opener = f"{URL}/{direct['id']}/open"
        assert_problem(await c.post(opener), 409, ErrorCode.INVALID_STATE)
        h.advance(timedelta(days=3))
        response = await c.post(opener)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "open"
        assert response.json()["expires_at"] == direct["expires_at"]
        assert response.json()["allowed_actions"] == ["edit", "cancel"]
        again = await c.post(opener)
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "open"
    assert await feed_items(h, direct["id"], live=True) == 1
    assert len(await h.pending_jobs("match_mentors")) == 1
    async with await h.client(target) as c:
        assert_problem(await c.post(opener), 403, ErrorCode.FORBIDDEN)
    async with await h.client(learner) as c:
        matched = await create(c, "backend", target_mentor_id=str(target.id))
        await write_request(h, matched["id"], status="matched")
        assert_problem(await c.post(f"{URL}/{matched['id']}/open"), 409, ErrorCode.INVALID_STATE)


async def test_expired_request_is_final(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        opened = await create(c)
        direct = await create(c, "backend", target_mentor_id=str(target.id))
    offer_ids = await add_offers(h, opened["id"], mentor, other)
    await write_request(h, opened["id"], expires_at=h.now - timedelta(minutes=1))
    async with h.db() as session:
        assert await expire_help_requests(session, h.now) == 1
    async with h.db() as session:
        row = await session.get(models.HelpRequest, UUID(opened["id"]))
        assert row is not None
        assert row.status == "expired"
    assert await offer_statuses(h, offer_ids) == {"closed"}
    expired = await notifications(h, "help_request_expired", learner.id)
    assert len(expired) == 1
    assert isinstance(expired[0].payload, dict)
    assert expired[0].payload["actions"] == [
        {
            "kind": "link",
            "label_key": "notifications.actions.reopen",
            "url": f"/mentorship/new?from={opened['id']}",
        }
    ]
    assert await feed_items(h, opened["id"], live=True) == 0
    async with await h.client(learner) as c:
        for action in ("cancel", "open"):
            assert_problem(
                await c.post(f"{URL}/{opened['id']}/{action}"), 409, ErrorCode.INVALID_STATE
            )
    async with await h.client(other) as c:
        for action in ("cancel", "decline", "open"):
            assert_problem(await c.post(f"{URL}/{opened['id']}/{action}"), 404, ErrorCode.NOT_FOUND)

    await write_request(h, direct["id"], expires_at=h.now - timedelta(minutes=1))
    async with h.db() as session:
        assert await expire_help_requests(session, h.now) == 1
    async with await h.client(target) as c:
        assert_problem(await c.post(f"{URL}/{direct['id']}/decline"), 409, ErrorCode.INVALID_STATE)


async def test_lifecycle_auth(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    async with await h.client(learner) as c:
        opened = await create(c)
    newcomer = await h.user(login="newcomer", onboarded=False)
    for action in ("cancel", "decline", "open"):
        path = f"{URL}/{opened['id']}/{action}"
        async with h.anonymous() as c:
            assert_problem(await c.post(path), 401, ErrorCode.UNAUTHENTICATED)
        async with await h.client(newcomer) as c:
            assert_problem(await c.post(path), 403, ErrorCode.ONBOARDING_REQUIRED)
