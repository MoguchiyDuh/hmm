import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from hmm_core.mentorship import service
from hmm_core.platform.errors import ProblemError
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

REQUESTS = "/api/v1/help-requests"
OFFERS = "/api/v1/help-offers"


def request_body(area_key: str = "backend", **fields: Any) -> dict[str, Any]:
    return {
        "area_key": area_key,
        "topic": "FastAPI auth",
        "goal": "ship a login flow",
        "format": "online",
        **fields,
    }


async def create_request(
    c: httpx.AsyncClient, area_key: str = "backend", **fields: Any
) -> dict[str, Any]:
    response = await c.post(REQUESTS, json=request_body(area_key, **fields))
    assert response.status_code == 201, response.text
    return response.json()


async def offer(c: httpx.AsyncClient, request_id: str, message: str = "Happy to help") -> Any:
    return await c.post(f"{REQUESTS}/{request_id}/offers", json={"message": message})


async def create_offer(c: httpx.AsyncClient, request_id: str) -> dict[str, Any]:
    response = await offer(c, request_id)
    assert response.status_code == 201, response.text
    return response.json()


async def set_score(
    h: Harness,
    user: models.User,
    area: models.Area,
    calculated: str,
    override: str | None = None,
) -> None:
    async with h.db() as session:
        row = await session.get(models.AreaScore, (user.id, area.id))
        if row is None:
            row = models.AreaScore(user_id=user.id, area_id=area.id, calc_reason="seed")
            session.add(row)
        row.calculated = Decimal(calculated)
        row.override = Decimal(override) if override is not None else None


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


async def notifications(h: Harness, user_id: UUID, type: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user_id, models.Notification.type == type
                )
            )
        )


async def offer_status(h: Harness, offer_id: str) -> str:
    async with h.db() as session:
        row = await session.get(models.HelpOffer, UUID(offer_id))
        assert row is not None
        return row.status


async def request_status(h: Harness, request_id: str) -> str:
    async with h.db() as session:
        row = await session.get(models.HelpRequest, UUID(request_id))
        assert row is not None
        return row.status


async def set_request_status(h: Harness, request_id: str, status: str) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.HelpRequest)
            .where(models.HelpRequest.id == UUID(request_id))
            .values(status=status)
        )


async def mentorship_count(h: Harness) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(models.Mentorship)) or 0


async def test_create_offer(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    await set_score(h, mentor, areas["backend"], "8.00")
    await set_score(h, learner, areas["backend"], "4.00")
    async with await h.client(learner) as c:
        opened = await create_request(c)
        direct = await create_request(c, "data", target_mentor_id=str(mentor.id))
        assert_problem(await offer(c, opened["id"]), 403, ErrorCode.FORBIDDEN)
    async with await h.client(mentor) as c:
        created = await create_offer(c, opened["id"])
    assert created["mentor_id"] == str(mentor.id)
    assert created["request_id"] == opened["id"]
    assert created["mentor_score"] == 8.0
    assert created["kind"] == "mentoring"
    assert created["status"] == "pending"
    assert created["past_mentorship_count"] == 0
    assert created["mentor_rating"] is None
    assert created["mentor_review_count"] == 0
    assert created["allowed_actions"] == ["withdraw"]
    received = await notifications(h, learner.id, "help_offer_received")
    assert len(received) == 1
    payload = received[0].payload
    assert isinstance(payload, dict)
    assert payload["mentor_score"] == 8.0
    assert payload["learner_score"] == 4.0
    assert payload["kind"] == "mentoring"
    assert payload["actions"] == [
        {
            "kind": "api",
            "key": "help_offer_accept",
            "entity_id": created["id"],
            "label_key": "notifications.actions.help_offer_accept",
        }
    ]
    async with await h.client(learner) as c:
        listed = await c.get(f"{REQUESTS}/{opened['id']}")
        assert [item["allowed_actions"] for item in listed.json()["offers"]] == [["accept"]]
    async with await h.client(other) as c:
        assert_problem(await offer(c, direct["id"]), 404, ErrorCode.NOT_FOUND)
    async with await h.client(mentor) as c:
        targeted = await create_offer(c, direct["id"])
        assert targeted["status"] == "pending"
        await set_request_status(h, direct["id"], "matched")
        assert_problem(await offer(c, direct["id"]), 409, ErrorCode.INVALID_STATE)
        await set_request_status(h, opened["id"], "matched")
        assert_problem(await offer(c, opened["id"]), 404, ErrorCode.NOT_FOUND)
        assert_problem(await offer(c, str(uuid4())), 404, ErrorCode.NOT_FOUND)


async def test_offer_capacity(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user(login="learner")
    busy = await h.user(login="busy")
    single = await h.user(login="single", mentor_capacity=1)
    for index, area_key in enumerate(("design", "qa", "data")):
        mentee = await h.user(login=f"mentee{index}")
        await add_active_mentorship(h, mentee, busy, areas[area_key])
    await add_active_mentorship(h, await h.user(login="pupil"), single, areas["design"])
    async with await h.client(learner) as c:
        opened = await create_request(c)
    async with await h.client(busy) as c:
        assert_problem(await offer(c, opened["id"]), 409, ErrorCode.MENTOR_CAPACITY_REACHED)
    async with await h.client(single) as c:
        assert_problem(await offer(c, opened["id"]), 409, ErrorCode.MENTOR_CAPACITY_REACHED)


async def test_offer_restricted(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    async with h.db() as session:
        session.add(
            models.MemberRestriction(
                user_id=mentor.id,
                kind="mentoring",
                reason="spam",
                created_by=admin.id,
                lifted_at=None,
            )
        )
    async with await h.client(learner) as c:
        opened = await create_request(c)
    async with await h.client(mentor) as c:
        assert_problem(await offer(c, opened["id"]), 403, ErrorCode.RESTRICTED)


async def test_withdraw_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        opened = await create_request(c)
    async with await h.client(mentor) as c:
        created = await create_offer(c, opened["id"])
    before = len(await notifications(h, learner.id, "help_offer_received"))
    async with await h.client(learner) as c:
        assert_problem(await c.delete(f"{OFFERS}/{created['id']}"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(other) as c:
        assert_problem(await c.delete(f"{OFFERS}/{created['id']}"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(mentor) as c:
        response = await c.delete(f"{OFFERS}/{created['id']}")
        assert response.status_code == 204, response.text
        assert await offer_status(h, created["id"]) == "withdrawn"
        assert_problem(await c.delete(f"{OFFERS}/{created['id']}"), 409, ErrorCode.INVALID_STATE)
        assert_problem(await c.delete(f"{OFFERS}/{uuid4()}"), 404, ErrorCode.NOT_FOUND)
        again = await create_offer(c, opened["id"])
        assert again["id"] == created["id"]
        assert again["status"] == "pending"
        repeat = await offer(c, opened["id"], "Different text")
        assert repeat.status_code == 201, repeat.text
        assert repeat.json()["id"] == created["id"]
        assert repeat.json()["message"] == "Happy to help"
    assert before == 1
    assert len(await notifications(h, learner.id, "help_offer_received")) == 1
    assert await offer_status(h, created["id"]) == "pending"


async def test_accept_offer(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    await set_score(h, mentor, areas["backend"], "8.00")
    await set_score(h, learner, areas["backend"], "4.00")
    async with await h.client(learner) as c:
        opened = await create_request(c)
    async with await h.client(mentor) as c:
        chosen = await create_offer(c, opened["id"])
    async with await h.client(other) as c:
        rival = await create_offer(c, opened["id"])
        assert_problem(await c.post(f"{OFFERS}/{chosen['id']}/accept"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(learner) as c:
        response = await c.post(f"{OFFERS}/{chosen['id']}/accept")
        assert response.status_code == 201, response.text
        mentorship = response.json()
        assert_problem(
            await c.post(f"{OFFERS}/{chosen['id']}/accept"), 409, ErrorCode.INVALID_STATE
        )
        assert_problem(await c.post(f"{OFFERS}/{uuid4()}/accept"), 404, ErrorCode.NOT_FOUND)
    assert mentorship["request_id"] == opened["id"]
    assert mentorship["mentor_id"] == str(mentor.id)
    assert mentorship["learner_id"] == str(learner.id)
    assert mentorship["area_key"] == "backend"
    assert mentorship["kind"] == "mentoring"
    assert mentorship["mentor_score_at_start"] == 8.0
    assert mentorship["learner_score_at_start"] == 4.0
    assert mentorship["status"] == "active"
    assert mentorship["outcome"] is None
    assert mentorship["mentor_trust"] == "self"
    assert mentorship["learner_trust"] == "self"
    assert mentorship["checkins"] == []
    assert mentorship["allowed_actions"] == ["check_in", "close", "cancel"]
    assert await request_status(h, opened["id"]) == "matched"
    assert await offer_status(h, chosen["id"]) == "accepted"
    assert await offer_status(h, rival["id"]) == "closed"
    closed = await notifications(h, other.id, "help_offer_closed")
    assert len(closed) == 1
    assert isinstance(closed[0].payload, dict)
    assert closed[0].payload["reason"] == "The learner accepted another mentor's offer."
    assert await notifications(h, mentor.id, "help_offer_closed") == []
    accepted = await notifications(h, mentor.id, "help_offer_accepted")
    assert len(accepted) == 1
    assert isinstance(accepted[0].payload, dict)
    assert accepted[0].payload["link"] == f"/mentorships/{mentorship['id']}"
    assert accepted[0].payload["mentorship_id"] == mentorship["id"]
    async with h.db() as session:
        live = await session.scalar(
            select(func.count())
            .select_from(models.FeedItem)
            .where(
                models.FeedItem.subject_id == UUID(opened["id"]),
                models.FeedItem.hidden_at.is_(None),
            )
        )
        assert live == 0


@pytest.mark.parametrize(
    ("mentor_score", "learner_score", "override", "kind"),
    [
        ("6.00", "4.00", None, "mentoring"),
        ("6.00", "4.50", None, "study_pair"),
        ("5.00", "4.00", "7.50", "mentoring"),
    ],
)
async def test_classification(
    harness: Harness, mentor_score: str, learner_score: str, override: str | None, kind: str
) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    await set_score(h, mentor, areas["backend"], mentor_score, override)
    await set_score(h, learner, areas["backend"], learner_score)
    async with await h.client(learner) as c:
        opened = await create_request(c)
    async with await h.client(mentor) as c:
        created = await create_offer(c, opened["id"])
        assert created["kind"] == kind
    async with await h.client(learner) as c:
        response = await c.post(f"{OFFERS}/{created['id']}/accept")
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == kind
    flipped = "study_pair" if kind == "mentoring" else "mentoring"
    await set_score(h, mentor, areas["backend"], "10.00" if flipped == "mentoring" else "0.00")
    async with h.db() as session:
        row = await session.get(models.Mentorship, UUID(response.json()["id"]))
        assert row is not None
        assert row.kind == kind


async def test_concurrent_accepts(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    first = await h.user(login="first")
    second = await h.user(login="second")
    async with await h.client(learner) as c:
        opened = await create_request(c)
    offer_ids: list[UUID] = []
    for mentor in (first, second):
        async with await h.client(mentor) as c:
            offer_ids.append(UUID((await create_offer(c, opened["id"]))["id"]))

    async def attempt(offer_id: UUID) -> Any:
        async with h.sessionmaker.begin() as session:
            return await service.accept_help_offer(session, learner, offer_id, h.now)

    results = await asyncio.gather(
        *(attempt(offer_id) for offer_id in offer_ids), return_exceptions=True
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], ProblemError)
    assert errors[0].code == ErrorCode.INVALID_STATE
    assert await mentorship_count(h) == 1


async def test_accept_rechecks_limits(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    mentor = await h.user(login="mentor", mentor_capacity=1)
    first = await h.user(login="first")
    second = await h.user(login="second")
    pending: list[tuple[models.User, str]] = []
    for learner in (first, second):
        async with await h.client(learner) as c:
            opened = await create_request(c)
        async with await h.client(mentor) as c:
            pending.append((learner, (await create_offer(c, opened["id"]))["id"]))
    for (learner, offer_id), expected in zip(pending, (201, 409), strict=True):
        async with await h.client(learner) as c:
            response = await c.post(f"{OFFERS}/{offer_id}/accept")
            if expected == 201:
                assert response.status_code == 201, response.text
            else:
                assert_problem(response, 409, ErrorCode.MENTOR_CAPACITY_REACHED)

    busy_learner = await h.user(login="busy")
    helper = await h.user(login="helper")
    async with await h.client(busy_learner) as c:
        opened = await create_request(c)
    async with await h.client(helper) as c:
        offer_id = (await create_offer(c, opened["id"]))["id"]
    for area_key in ("design", "qa"):
        await add_active_mentorship(h, busy_learner, await h.user(), areas[area_key])
    async with await h.client(busy_learner) as c:
        assert_problem(
            await c.post(f"{OFFERS}/{offer_id}/accept"), 409, ErrorCode.MENTEE_LIMIT_REACHED
        )


async def test_accept_roles(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        direct = await create_request(c, target_mentor_id=str(target.id))
    async with await h.client(target) as c:
        created = await create_offer(c, direct["id"])
        assert_problem(await c.post(f"{OFFERS}/{created['id']}/accept"), 403, ErrorCode.FORBIDDEN)
    async with await h.client(other) as c:
        assert_problem(await c.post(f"{OFFERS}/{created['id']}/accept"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(learner) as c:
        response = await c.post(f"{OFFERS}/{created['id']}/accept")
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "study_pair"


async def test_help_offers_auth(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    newcomer = await h.user(login="newcomer", onboarded=False)
    paths = [
        ("post", f"{REQUESTS}/{uuid4()}/offers", {"message": "hi"}),
        ("delete", f"{OFFERS}/{uuid4()}", None),
        ("post", f"{OFFERS}/{uuid4()}/accept", None),
    ]
    for method, path, payload in paths:
        async with h.anonymous() as c:
            assert_problem(
                await c.request(method, path, json=payload), 401, ErrorCode.UNAUTHENTICATED
            )
        async with await h.client(newcomer) as c:
            assert_problem(
                await c.request(method, path, json=payload), 403, ErrorCode.ONBOARDING_REQUIRED
            )
