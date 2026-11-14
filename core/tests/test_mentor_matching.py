import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts.algo import AlgoContext, MentorMatchRequest, Rec
from hmm_contracts.common import ErrorCode
from hmm_core import models
from hmm_core.mentorship.jobs import match_mentors
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

REQUESTS = "/api/v1/help-requests"
TOP_N = 5


class FailingAlgorithm(FakeAlgorithm):
    async def match_mentors(self, ctx: AlgoContext, request: MentorMatchRequest) -> list[Rec]:
        raise RuntimeError("boom")


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


async def scored(
    h: Harness, area: models.Area, score: str, login: str | None = None, **fields: Any
) -> models.User:
    user = await h.user(login=login, **fields)
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=user.id, area_id=area.id, calculated=Decimal(score), calc_reason="seed"
            )
        )
    return user


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


async def recommendation_sets(h: Harness, request_id: str) -> list[models.RecommendationSet]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.RecommendationSet).where(
                    models.RecommendationSet.subject_type == "help_request",
                    models.RecommendationSet.subject_id == UUID(request_id),
                )
            )
        )


async def matches(h: Harness) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification)
                .where(models.Notification.type == "help_request_match")
                .order_by(models.Notification.created_at, models.Notification.id)
            )
        )


async def live_feed_items(h: Harness, request_id: str) -> int:
    async with h.db() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(models.FeedItem)
                .where(
                    models.FeedItem.subject_id == UUID(request_id),
                    models.FeedItem.hidden_at.is_(None),
                )
            )
            or 0
        )


async def test_match_mentors_job(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await scored(h, areas["backend"], "2.00", login="learner")
    for index in range(TOP_N + 2):
        await scored(h, areas["backend"], f"{index + 3}.00", login=f"mentor{index}")
    async with await h.client(learner) as c:
        created = await create_request(c)
    jobs = await h.pending_jobs("match_mentors")
    assert len(jobs) == 1
    assert jobs[0]["lock"] == f"mentor:{created['id']}"
    await h.run_jobs()
    rows = await recommendation_sets(h, created["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ready"
    assert row.algo_version == h.algorithm.version
    assert re.fullmatch(r"[0-9a-f]{64}", row.input_hash)
    assert row.enrolled is None
    assert isinstance(row.others, list)
    assert len(row.others) == TOP_N
    scores = [Rec.model_validate(item).score for item in row.others]
    assert scores == sorted(scores, reverse=True)
    notified = await matches(h)
    assert len(notified) == TOP_N
    assert {str(n.user_id) for n in notified} == {
        item["user_id"] for item in row.others if isinstance(item, dict)
    }
    for notification in notified:
        payload = notification.payload
        assert isinstance(payload, dict)
        assert payload["link"] == f"/help-requests/{created['id']}"
        assert isinstance(payload["reason"], str) and payload["reason"]
        assert payload["actions"] == [
            {"kind": "link", "label_key": "notifications.actions.help", "url": "/mentorship"}
        ]

    await h.run_jobs()
    await match_mentors(h.runtime, created["id"])
    assert len(await recommendation_sets(h, created["id"])) == 1
    assert len(await matches(h)) == TOP_N


async def test_candidate_eligibility(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    backend = areas["backend"]
    admin = await h.admin()
    learner = await scored(h, backend, "2.00", login="learner")
    excluded = [
        await scored(h, backend, "9.00", login="banned", banned_at=h.now),
        await scored(h, backend, "9.00", login="dnd", dnd_until=h.now + timedelta(days=1)),
        await scored(h, backend, "9.00", login="newcomer", onboarded=False),
        await scored(h, backend, "9.00", login="busy"),
        await scored(h, backend, "9.00", login="restricted"),
    ]
    for _ in range(3):
        await add_active_mentorship(h, await h.user(), excluded[3], areas["qa"])
    async with h.db() as session:
        session.add(
            models.MemberRestriction(
                user_id=excluded[4].id,
                kind="mentoring",
                reason="spam",
                created_by=admin.id,
                lifted_at=None,
            )
        )
    plain = await scored(h, backend, "7.00", login="plain")
    offer_only = await h.user(login="offering")
    async with h.db() as session:
        session.add(
            models.MentorOffer(
                user_id=offer_only.id, area_id=backend.id, topics="auth", active=True
            )
        )
    await h.user(login="unscored")
    extras = [
        await scored(h, backend, f"{index + 4}.00", login=f"extra{index}") for index in range(3)
    ]
    async with await h.client(learner) as c:
        created = await create_request(c)
    await h.run_jobs()
    rows = await recommendation_sets(h, created["id"])
    assert len(rows) == 1
    assert isinstance(rows[0].others, list)
    recommended = {item["user_id"] for item in rows[0].others if isinstance(item, dict)}
    eligible = {str(user.id) for user in (plain, offer_only, *extras)}
    assert recommended == eligible
    notified = {str(n.user_id) for n in await matches(h)}
    assert notified == eligible
    assert str(learner.id) not in notified
    assert not notified & {str(user.id) for user in excluded}


async def test_direct_request_is_skipped(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await scored(h, areas["backend"], "2.00", login="learner")
    target = await scored(h, areas["backend"], "9.00", login="target")
    async with await h.client(learner) as c:
        created = await create_request(c, target_mentor_id=str(target.id))
    assert await h.pending_jobs("match_mentors") == []
    await match_mentors(h.runtime, created["id"])
    await match_mentors(h.runtime, str(uuid4()))
    assert await recommendation_sets(h, created["id"]) == []
    assert await matches(h) == []


async def test_match_failure(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await scored(h, areas["backend"], "2.00", login="learner")
    await scored(h, areas["backend"], "8.00", login="mentor")
    async with await h.client(learner) as c:
        created = await create_request(c)
    h.runtime = replace(h.runtime, algorithm=FailingAlgorithm())
    await h.run_jobs()
    assert await matches(h) == []
    rows = await recommendation_sets(h, created["id"])
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error == "boom"
    assert rows[0].others is None
    async with h.db() as session:
        alerts = list(await session.scalars(select(models.AdminAlert)))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.type == "algo_failure"
    assert alert.status == "open"
    assert alert.subject_type == "help_request"
    assert alert.subject_id == UUID(created["id"])
    assert alert.dedupe_key == (f"algo_failure:match_mentors:{created['id']}:{h.now:%Y-%m-%d-%H}")
    assert await live_feed_items(h, created["id"]) == 1
    async with await h.client(learner) as c:
        response = await c.get(f"{REQUESTS}/{created['id']}/suggested-mentors")
        assert response.status_code == 200, response.text
        assert response.json() == []


async def test_suggested_mentors_read(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await scored(h, areas["backend"], "2.00", login="learner")
    mentors = [
        await scored(h, areas["backend"], f"{9 - index}.00", login=f"mentor{index}")
        for index in range(4)
    ]
    other = await h.user(login="other")
    async with await h.client(learner) as c:
        created = await create_request(c)
        url = f"{REQUESTS}/{created['id']}/suggested-mentors"
        before = await c.get(url)
        assert before.status_code == 200, before.text
        assert before.json() == []
        await h.run_jobs()
        response = await c.get(url)
        assert response.status_code == 200, response.text
        listed = response.json()
        assert [item["user_id"] for item in listed] == [str(mentor.id) for mentor in mentors]
        assert [item["profile"]["display_name"] for item in listed] == [
            mentor.display_name for mentor in mentors
        ]
        assert listed[0]["profile"]["id"] == str(mentors[0].id)
        assert listed[0]["reasons"]
        async with h.db() as session:
            await session.execute(
                update(models.User).where(models.User.id == mentors[0].id).values(banned_at=h.now)
            )
            await session.execute(
                update(models.User)
                .where(models.User.id == mentors[1].id)
                .values(dnd_until=h.now + timedelta(days=1))
            )
        filtered = await c.get(url)
        assert [item["user_id"] for item in filtered.json()] == [
            str(mentor.id) for mentor in mentors[2:]
        ]
        direct = await create_request(c, "data", target_mentor_id=str(mentors[2].id))
        assert_problem(
            await c.get(f"{REQUESTS}/{uuid4()}/suggested-mentors"), 404, ErrorCode.NOT_FOUND
        )
    async with await h.client(other) as c:
        assert_problem(await c.get(url), 403, ErrorCode.FORBIDDEN)
        assert_problem(
            await c.get(f"{REQUESTS}/{direct['id']}/suggested-mentors"), 404, ErrorCode.NOT_FOUND
        )
    async with h.db() as session:
        await session.execute(
            update(models.HelpRequest)
            .where(models.HelpRequest.id == UUID(created["id"]))
            .values(status="cancelled")
        )
    async with await h.client(learner) as c:
        cancelled = await c.get(url)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get(url), 401, ErrorCode.UNAUTHENTICATED)
