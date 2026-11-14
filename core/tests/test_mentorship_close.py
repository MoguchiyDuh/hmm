from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


def url(mentorship_id: UUID, action: str = "") -> str:
    base = f"/api/v1/mentorships/{mentorship_id}"
    return f"{base}/{action}" if action else base


class Scenario:
    def __init__(
        self,
        h: Harness,
        learner: models.User,
        mentor: models.User,
        other: models.User,
        admin: models.User,
    ) -> None:
        self.h = h
        self.learner = learner
        self.mentor = mentor
        self.other = other
        self.admin = admin

    async def mentorship(
        self,
        kind: str = "mentoring",
        *,
        learner: models.User | None = None,
        **fields: Any,
    ) -> UUID:
        h = self.h
        learner = learner or self.learner
        async with h.db() as session:
            area_id = await session.scalar(
                select(models.Area.id).order_by(models.Area.sort).limit(1)
            )
            assert area_id is not None
            request = models.HelpRequest(
                learner_id=learner.id,
                area_id=area_id,
                topic="FastAPI auth",
                goal="ship a login flow",
                format="online",
                status="matched",
                expires_at=h.now + timedelta(days=14),
            )
            session.add(request)
            await session.flush()
            row = models.Mentorship(
                request_id=request.id,
                mentor_id=self.mentor.id,
                learner_id=learner.id,
                area_id=area_id,
                kind=kind,
                mentor_score_at_start=Decimal("5.0"),
                learner_score_at_start=Decimal("5.0"),
                status=fields.pop("status", "active"),
                started_at=h.now - timedelta(days=20),
                mentor_trust="self",
                learner_trust="self",
                **fields,
            )
            session.add(row)
            await session.flush()
            return row.id


async def scenario(h: Harness) -> Scenario:
    await h.seed_areas()
    return Scenario(
        h,
        await h.user(login="learner"),
        await h.user(login="mentor"),
        await h.user(login="other"),
        await h.admin(),
    )


async def post(h: Harness, user: models.User, path: str, body: dict[str, Any] | None = None) -> Any:
    async with await h.client(user) as c:
        return await c.post(path, json=body or {})


async def get(h: Harness, user: models.User, mentorship_id: UUID) -> dict[str, Any]:
    async with await h.client(user) as c:
        response = await c.get(url(mentorship_id))
        assert response.status_code == 200, response.text
        return response.json()


async def confirmations(h: Harness, mentorship_id: UUID) -> list[models.Confirmation]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Confirmation)
                .where(models.Confirmation.fact_id == mentorship_id)
                .order_by(models.Confirmation.created_at, models.Confirmation.id)
            )
        )


async def windows(h: Harness, mentorship_id: UUID) -> list[models.ReviewWindow]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.ReviewWindow).where(
                    models.ReviewWindow.subject_type == "mentorship",
                    models.ReviewWindow.subject_id == mentorship_id,
                )
            )
        )


async def notices(h: Harness, user: models.User, type: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user.id, models.Notification.type == type
                )
            )
        )


def payload(row: models.Notification) -> dict[str, Any]:
    return cast(dict[str, Any], row.payload)


async def completed_items(h: Harness, mentorship_id: UUID) -> int:
    async with h.db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.FeedItem)
            .where(
                models.FeedItem.kind == "mentorship_completed",
                models.FeedItem.subject_type == "mentorship",
                models.FeedItem.subject_id == mentorship_id,
            )
        )
        return int(count or 0)


async def test_learner_close_reached(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    response = await post(
        h, s.learner, url(mentorship_id, "close"), {"outcome": "reached", "summary": "shipped"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "closed"
    assert body["outcome"] == "reached"
    assert body["summary"] == "shipped"
    assert datetime.fromisoformat(body["closed_at"]) == h.now
    assert body["learner_trust"] == "self"
    assert body["mentor_trust"] == "trusted"
    assert body["allowed_actions"] == []
    assert (await get(h, s.mentor, mentorship_id))["allowed_actions"] == ["acknowledge"]
    rows = await confirmations(h, mentorship_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.fact_type == "mentorship_side"
    assert row.confirmer_id == s.learner.id
    assert row.subject_user_id == s.mentor.id
    assert row.source == "learner"
    assert row.withdrawn_at is None
    assert row.ignored is False
    found = await windows(h, mentorship_id)
    assert len(found) == 1
    assert found[0].revealed_at is None
    assert found[0].participants == {str(s.mentor.id): "mentor", str(s.learner.id): "mentor"}
    for user in (s.mentor, s.learner):
        assert len(await notices(h, user, "reviews_open")) == 1
        assert len(await notices(h, user, "mentorship_closed")) == 1
    actions = payload((await notices(h, s.mentor, "mentorship_closed"))[0])["actions"]
    assert [(action["kind"], action.get("key"), action.get("label_key")) for action in actions] == [
        ("api", "mentorship_acknowledge", "notifications.actions.mentorship_acknowledge"),
        ("link", None, "notifications.actions.endorse"),
    ]
    assert payload((await notices(h, s.learner, "mentorship_closed"))[0])["actions"] == []
    assert await completed_items(h, mentorship_id) == 1
    assert len(await h.pending_jobs("recompute_member")) == 2


async def test_close_without_reached_outcome(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    for outcome in ("partial", "not_reached"):
        learner = await h.user()
        mentorship_id = await s.mentorship(learner=learner)
        response = await post(h, learner, url(mentorship_id, "close"), {"outcome": outcome})
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == outcome
        assert await completed_items(h, mentorship_id) == 0
        assert len(await windows(h, mentorship_id)) == 1
        assert len(await confirmations(h, mentorship_id)) == 1


async def test_study_pair_close_and_acknowledge(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship("study_pair")
    response = await post(h, s.learner, url(mentorship_id, "close"), {"outcome": "reached"})
    assert response.status_code == 200, response.text
    assert response.json()["mentor_trust"] == "peer"
    assert [row.source for row in await confirmations(h, mentorship_id)] == ["peer"]
    found = await windows(h, mentorship_id)
    assert isinstance(found[0].participants, dict)
    assert set(found[0].participants.values()) == {"member"}
    response = await post(h, s.mentor, url(mentorship_id, "acknowledge"), {})
    assert response.status_code == 200, response.text
    assert response.json()["learner_trust"] == "peer"
    learner_side = [
        row for row in await confirmations(h, mentorship_id) if row.subject_user_id == s.learner.id
    ]
    assert [row.source for row in learner_side] == ["peer"]


async def test_close_permissions_and_state(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    body = {"outcome": "reached"}
    assert_problem(
        await post(h, s.mentor, url(mentorship_id, "close"), body), 403, ErrorCode.FORBIDDEN
    )
    assert_problem(
        await post(h, s.other, url(mentorship_id, "close"), body), 404, ErrorCode.NOT_FOUND
    )
    assert_problem(await post(h, s.learner, url(uuid4(), "close"), body), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        assert_problem(
            await c.post(url(mentorship_id, "close"), json=body), 401, ErrorCode.UNAUTHENTICATED
        )
    assert (await post(h, s.learner, url(mentorship_id, "close"), body)).status_code == 200
    assert_problem(
        await post(h, s.learner, url(mentorship_id, "close"), body), 409, ErrorCode.INVALID_STATE
    )
    second = await h.user()
    cancelled_id = await s.mentorship(learner=second, status="cancelled")
    assert_problem(
        await post(h, second, url(cancelled_id, "close"), body), 409, ErrorCode.INVALID_STATE
    )


async def test_propose_close(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    response = await post(h, s.mentor, url(mentorship_id, "propose-close"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "closing"
    assert datetime.fromisoformat(body["close_proposed_at"]) == h.now
    assert body["allowed_actions"] == ["cancel"]
    assert (await get(h, s.learner, mentorship_id))["allowed_actions"] == ["close", "cancel"]
    proposed = await notices(h, s.learner, "mentorship_close_proposed")
    assert len(proposed) == 1
    data = payload(proposed[0])
    assert datetime.fromisoformat(data["respond_by"]) == h.now + timedelta(days=7)
    actions = data["actions"]
    assert [action["label_key"] for action in actions] == [
        "notifications.actions.close_with_outcome"
    ]
    assert_problem(
        await post(h, s.mentor, url(mentorship_id, "propose-close")), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await post(h, s.learner, url(mentorship_id, "propose-close")), 403, ErrorCode.FORBIDDEN
    )
    assert_problem(
        await post(h, s.other, url(mentorship_id, "propose-close")), 404, ErrorCode.NOT_FOUND
    )
    second = await h.user()
    closed_id = await s.mentorship(learner=second, status="closed", closed_at=h.now)
    assert_problem(
        await post(h, s.mentor, url(closed_id, "propose-close")), 409, ErrorCode.INVALID_STATE
    )


async def test_acknowledge(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    assert (
        await post(h, s.learner, url(mentorship_id, "close"), {"outcome": "reached"})
    ).status_code == 200
    assert_problem(
        await post(h, s.learner, url(mentorship_id, "acknowledge"), {}), 403, ErrorCode.FORBIDDEN
    )
    response = await post(
        h, s.mentor, url(mentorship_id, "acknowledge"), {"endorsement": "great learner"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert datetime.fromisoformat(body["mentor_acknowledged_at"]) == h.now
    assert body["endorsement"] == "great learner"
    assert body["learner_trust"] == "trusted"
    assert body["allowed_actions"] == []
    learner_side = [
        row for row in await confirmations(h, mentorship_id) if row.subject_user_id == s.learner.id
    ]
    assert [(row.source, row.confirmer_id) for row in learner_side] == [("mentor", s.mentor.id)]
    assert_problem(
        await post(h, s.mentor, url(mentorship_id, "acknowledge"), {}),
        409,
        ErrorCode.INVALID_STATE,
    )
    second = await h.user()
    closed_id = await s.mentorship(learner=second, status="closed", closed_at=h.now)
    assert_problem(
        await post(h, s.mentor, url(closed_id, "acknowledge"), {}), 409, ErrorCode.INVALID_STATE
    )


async def test_cancel(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    response = await post(h, s.learner, url(mentorship_id, "cancel"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["outcome"] is None
    assert body["closed_at"] is None
    assert body["allowed_actions"] == []
    assert await windows(h, mentorship_id) == []
    assert await notices(h, s.learner, "mentorship_closed") == []
    assert await notices(h, s.mentor, "mentorship_closed") == []
    assert await confirmations(h, mentorship_id) == []
    assert await completed_items(h, mentorship_id) == 0
    assert_problem(
        await post(h, s.learner, url(mentorship_id, "cancel")), 409, ErrorCode.INVALID_STATE
    )

    second = await h.user()
    by_mentor = await s.mentorship(learner=second)
    assert (await post(h, s.mentor, url(by_mentor, "cancel"))).status_code == 200

    third = await h.user()
    checked = await s.mentorship(learner=third)
    response = await post(
        h,
        third,
        url(checked, "checkins"),
        {"note": "fixed auth", "occurred_on": h.now.date().isoformat()},
    )
    assert response.status_code == 201, response.text
    assert_problem(await post(h, third, url(checked, "cancel")), 409, ErrorCode.INVALID_STATE)

    fourth = await h.user()
    closing = await s.mentorship(learner=fourth, status="closing", close_proposed_at=h.now)
    assert_problem(await post(h, s.other, url(closing, "cancel")), 404, ErrorCode.NOT_FOUND)
    assert (await post(h, fourth, url(closing, "cancel"))).status_code == 200

    fifth = await h.user()
    closed_id = await s.mentorship(learner=fifth, status="closed", closed_at=h.now)
    assert_problem(await post(h, fifth, url(closed_id, "cancel")), 409, ErrorCode.INVALID_STATE)
