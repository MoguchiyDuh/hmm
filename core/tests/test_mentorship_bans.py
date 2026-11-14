from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
from harness import Harness
from hmm_contracts.common import AuditAction, ErrorCode
from hmm_core import models
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

REQUESTS = "/api/v1/help-requests"


def ban_url(user: models.User) -> str:
    return f"/api/v1/admin/users/{user.id}/ban"


async def create_request(
    c: httpx.AsyncClient, area_key: str = "backend", **fields: Any
) -> dict[str, Any]:
    response = await c.post(
        REQUESTS,
        json={
            "area_key": area_key,
            "topic": "FastAPI auth",
            "goal": "ship a login flow",
            "format": "online",
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def ban(h: Harness, admin: models.User, user: models.User) -> None:
    async with await h.client(admin) as c:
        response = await c.post(ban_url(user), json={"reason": "spam"})
        assert response.status_code == 200, response.text


async def add_offer(h: Harness, request_id: UUID, mentor: models.User, status: str) -> UUID:
    async with h.db() as session:
        row = models.HelpOffer(
            request_id=request_id, mentor_id=mentor.id, message="I can help", status=status
        )
        session.add(row)
        await session.flush()
        return row.id


async def offer_status(h: Harness, offer_id: UUID) -> str:
    async with h.db() as session:
        row = await session.get(models.HelpOffer, offer_id)
        assert row is not None
        return row.status


async def request_row(h: Harness, request_id: str | UUID) -> models.HelpRequest:
    async with h.db() as session:
        row = await session.get(models.HelpRequest, UUID(str(request_id)))
        assert row is not None
        return row


async def count(h: Harness, model: type[models.Notification] | type[models.AuditLog]) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def notifications(h: Harness, user_id: UUID, type: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user_id, models.Notification.type == type
                )
            )
        )


async def ban_audits(h: Harness) -> int:
    async with h.db() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(models.AuditLog)
                .where(models.AuditLog.action == AuditAction.USER_BAN)
            )
            or 0
        )


async def mentor_offers(h: Harness, user_id: UUID) -> dict[UUID, bool]:
    async with h.db() as session:
        rows = await session.scalars(
            select(models.MentorOffer).where(models.MentorOffer.user_id == user_id)
        )
        return {row.area_id: row.active for row in rows}


async def test_ban_learner_cancels_requests(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    helper = await h.user(login="helper")
    mentor = await h.user(login="mentor")
    async with await h.client(learner) as c:
        opened = await create_request(c)
    offer_ids = [
        await add_offer(h, UUID(opened["id"]), author, "pending") for author in (helper, mentor)
    ]
    await ban(h, admin, learner)
    assert (await request_row(h, opened["id"])).status == "cancelled"
    assert [await offer_status(h, offer_id) for offer_id in offer_ids] == ["closed", "closed"]
    for author in (helper, mentor):
        closed = await notifications(h, author.id, "help_offer_closed")
        assert len(closed) == 1
        assert isinstance(closed[0].payload, dict)
        assert closed[0].payload["reason"] == "The learner was banned."
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
    assert await ban_audits(h) == 1


async def test_ban_mentor_withdraws_offers(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    async with h.db() as session:
        session.add_all(
            [
                models.MentorOffer(
                    user_id=mentor.id, area_id=areas["backend"].id, topics="auth", active=True
                ),
                models.MentorOffer(
                    user_id=mentor.id, area_id=areas["data"].id, topics="sql", active=False
                ),
            ]
        )
    async with await h.client(learner) as c:
        opened = await create_request(c)
    offer_id = await add_offer(h, UUID(opened["id"]), mentor, "pending")
    learner_before = await count_for(h, learner.id)
    await ban(h, admin, mentor)
    assert await mentor_offers(h, mentor.id) == {
        areas["backend"].id: False,
        areas["data"].id: False,
    }
    assert await offer_status(h, offer_id) == "withdrawn"
    assert (await request_row(h, opened["id"])).status == "open"
    assert await count_for(h, learner.id) == learner_before
    notifications_before = await count(h, models.Notification)
    audits_before = await count(h, models.AuditLog)
    await ban(h, admin, mentor)
    assert await count(h, models.Notification) == notifications_before
    assert await count(h, models.AuditLog) == audits_before
    assert await ban_audits(h) == 1


async def count_for(h: Harness, user_id: UUID) -> int:
    async with h.db() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(models.Notification)
                .where(models.Notification.user_id == user_id)
            )
            or 0
        )


async def test_ban_target_leaves_direct_request(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    target = await h.user(login="target")
    async with await h.client(learner) as c:
        direct = await create_request(c, target_mentor_id=str(target.id))
    async with h.db() as session:
        await session.execute(
            update(models.HelpRequest)
            .where(models.HelpRequest.id == UUID(direct["id"]))
            .values(created_at=h.now)
        )
    await ban(h, admin, target)
    row = await request_row(h, direct["id"])
    assert row.status == "direct"
    assert row.target_mentor_id == target.id
    h.advance(timedelta(days=3))
    async with await h.client(learner) as c:
        response = await c.post(f"{REQUESTS}/{direct['id']}/open")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "open"


async def test_ban_keeps_settled_rows(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    banned = await h.user(login="banned")
    async with h.db() as session:
        own = models.HelpRequest(
            learner_id=banned.id,
            area_id=areas["backend"].id,
            topic="Own",
            goal="Own goal",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=14),
        )
        other = models.HelpRequest(
            learner_id=learner.id,
            area_id=areas["data"].id,
            topic="Other",
            goal="Other goal",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=14),
        )
        session.add_all([own, other])
        await session.flush()
        own_id, other_id = own.id, other.id
    accepted = await add_offer(h, other_id, banned, "accepted")
    own_offer = await add_offer(h, own_id, learner, "accepted")
    before = await count(h, models.Notification)
    await ban(h, admin, banned)
    assert (await request_row(h, own_id)).status == "matched"
    assert (await request_row(h, other_id)).status == "matched"
    assert await offer_status(h, accepted) == "accepted"
    assert await offer_status(h, own_offer) == "accepted"
    assert await count(h, models.Notification) == before


async def test_unban_restores_nothing(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    admin = await h.admin()
    learner = await h.user(login="learner")
    async with h.db() as session:
        session.add(
            models.MentorOffer(
                user_id=learner.id, area_id=areas["qa"].id, topics="pytest", active=True
            )
        )
    async with await h.client(learner) as c:
        opened = await create_request(c)
    await ban(h, admin, learner)
    async with await h.client(admin) as c:
        response = await c.post(f"/api/v1/admin/users/{learner.id}/unban")
        assert response.status_code == 200, response.text
    async with h.db() as session:
        user = await session.get(models.User, learner.id)
        assert user is not None
        assert user.banned_at is None
    assert await mentor_offers(h, learner.id) == {areas["qa"].id: False}
    assert (await request_row(h, opened["id"])).status == "cancelled"


async def test_ban_auth(harness: Harness) -> None:
    h = harness
    member = await h.user(login="member")
    victim = await h.user(login="victim")
    async with await h.client(member) as c:
        assert_problem(
            await c.post(ban_url(victim), json={"reason": "spam"}), 403, ErrorCode.FORBIDDEN
        )
    async with h.anonymous() as c:
        assert_problem(
            await c.post(ban_url(victim), json={"reason": "spam"}), 401, ErrorCode.UNAUTHENTICATED
        )
