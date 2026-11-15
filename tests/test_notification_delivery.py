import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from harness import Harness, assert_problem
from sqlalchemy import select, text, update

from hmm import models
from hmm.common import DeliveryStatus, ErrorCode, NotificationChannel
from hmm.notifications import service
from hmm.notifications.payloads import (
    DeliveryAckResult,
    EventCancelledPayload,
    Notification,
    NotificationPayload,
    NotificationType,
    PendingDelivery,
)

INBOX = "/api/v1/notifications"
FIXTURES = Path(__file__).resolve().parent / "fixtures/notifications"


def payload() -> EventCancelledPayload:
    return EventCancelledPayload(
        link="/events",
        event_id=uuid4(),
        event_title="Event",
        reason="Cancelled",
    )


async def emit(h: Harness, user: models.User, body: NotificationPayload | None = None) -> UUID:
    async with h.db() as session:
        row = await service.notify(session, user.id, body or payload(), h.now)
        assert row is not None
        row.created_at = h.now
        return row.id


async def delivery(h: Harness, id: UUID) -> models.NotificationDelivery:
    async with h.db() as session:
        row = await session.get(models.NotificationDelivery, (id, NotificationChannel.TELEGRAM))
        assert row is not None
        return row


async def lease(h: Harness, limit: int = 50) -> list[PendingDelivery]:
    async with h.db() as session:
        return await service.lease_deliveries(session, h.now, limit)


async def ack(h: Harness, *results: DeliveryAckResult) -> None:
    async with h.db() as session:
        await service.acknowledge(session, h.now, list(results))


async def assert_count(client: httpx.AsyncClient, count: int) -> None:
    response = await client.get(f"{INBOX}/unread-count")
    assert response.status_code == 200, response.text
    assert response.json() == {"count": count}
    me = await client.get("/api/v1/me")
    assert me.status_code == 200, me.text
    assert me.json()["unread_count"] == count


async def test_inbox_privacy_pagination_and_filters(harness: Harness) -> None:
    h = harness
    user = await h.user()
    other = await h.user()
    other_id = await emit(h, other)
    ids: list[UUID] = []
    for _ in range(5):
        ids.append(await emit(h, user))
        h.advance(timedelta(seconds=1))
    async with await h.client(user) as client:
        seen: list[str] = []
        cursor = None
        sizes: list[int] = []
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(INBOX, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            sizes.append(len(page["items"]))
            for item in page["items"]:
                assert item["user_id"] == str(user.id)
                assert item["allowed_actions"] == ["read"]
                seen.append(item["id"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert sizes == [2, 2, 1]
        assert seen == [str(id) for id in reversed(ids)]
        assert len(set(seen)) == 5
        response = await client.post(f"{INBOX}/{other_id}/read")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        assert_problem(await client.post(f"{INBOX}/{uuid4()}/read"), 404, ErrorCode.NOT_FOUND)
        await assert_count(client, 5)
        assert (await client.post(f"{INBOX}/{ids[0]}/read")).status_code == 204
        await assert_count(client, 4)
        unread = await client.get(INBOX, params={"unread": "true"})
        assert [item["id"] for item in unread.json()["items"]] == [
            str(id) for id in reversed(ids[1:])
        ]
        read = await client.get(INBOX, params={"unread": "false"})
        assert len(read.json()["items"]) == 1
        item = read.json()["items"][0]
        assert item["id"] == str(ids[0])
        assert item["allowed_actions"] == []
        read_at = item["read_at"]
        h.advance(timedelta(seconds=20))
        assert (await client.post(f"{INBOX}/{ids[0]}/read")).status_code == 204
        read = await client.get(INBOX, params={"unread": "false"})
        assert read.json()["items"][0]["read_at"] == read_at
        await assert_count(client, 4)
        assert (await client.post(f"{INBOX}/read-all")).status_code == 204
        await assert_count(client, 0)
        assert (await client.get(INBOX, params={"unread": "true"})).json()["items"] == []
    async with await h.client(other) as client:
        await assert_count(client, 1)


async def test_inbox_before_onboarding_and_anonymous(harness: Harness) -> None:
    h = harness
    user = await h.user(onboarded=False)
    id = await emit(h, user)
    async with await h.client(user) as client:
        response = await client.get(INBOX)
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["id"] == str(id)
        await assert_count(client, 1)
        assert (await client.post(f"{INBOX}/{id}/read")).status_code == 204
        assert (await client.post(f"{INBOX}/read-all")).status_code == 204
        await assert_count(client, 0)
    async with h.anonymous() as client:
        for method, path in (
            ("GET", INBOX),
            ("GET", f"{INBOX}/unread-count"),
            ("POST", f"{INBOX}/{id}/read"),
            ("POST", f"{INBOX}/read-all"),
        ):
            assert_problem(await client.request(method, path), 401, ErrorCode.UNAUTHENTICATED)


async def test_pending_lease_and_redelivery(harness: Harness) -> None:
    h = harness
    user = await h.user(locale="ru")
    tg = await h.link_telegram(user)
    async with h.db() as session:
        await session.execute(
            update(models.TelegramLink)
            .where(models.TelegramLink.user_id == user.id)
            .values(chat_id=-123456)
        )
    body = payload()
    id = await emit(h, user, body)
    items = await lease(h)
    assert len(items) == 1
    pending = items[0]
    assert pending.delivery_id == id
    assert pending.notification_id == id
    assert pending.telegram_chat_id == -123456
    assert pending.telegram_chat_id != tg
    assert pending.locale == "ru"
    assert pending.type == body.type
    assert pending.payload == body
    assert pending.created_at == h.now
    row = await delivery(h, id)
    assert row.next_attempt_at == h.now + timedelta(seconds=60)
    assert row.attempts == 0
    assert await lease(h) == []
    h.advance(timedelta(seconds=61))
    assert await lease(h) == items
    assert (await delivery(h, id)).attempts == 0


async def test_pending_excludes_unavailable_and_expired(harness: Harness) -> None:
    h = harness
    ids: dict[str, UUID] = {}
    for state in ("future", "broken", "banned", "expired", "boundary", "ready"):
        user = await h.user()
        await h.link_telegram(user)
        ids[state] = await emit(h, user)
        async with h.db() as session:
            if state == "future":
                await session.execute(
                    update(models.NotificationDelivery)
                    .where(models.NotificationDelivery.notification_id == ids[state])
                    .values(next_attempt_at=h.now + timedelta(seconds=1))
                )
            elif state == "broken":
                await session.execute(
                    update(models.TelegramLink)
                    .where(models.TelegramLink.user_id == user.id)
                    .values(broken_at=h.now)
                )
            elif state == "banned":
                await session.execute(
                    update(models.User).where(models.User.id == user.id).values(banned_at=h.now)
                )
            elif state in {"expired", "boundary"}:
                created_at = h.now - timedelta(days=7, seconds=1 if state == "expired" else 0)
                for table in ("notifications", "notification_deliveries"):
                    column = "id" if table == "notifications" else "notification_id"
                    await session.execute(
                        text(f"UPDATE {table} SET created_at = :created_at WHERE {column} = :id"),
                        {"created_at": created_at, "id": ids[state]},
                    )
    assert {item.delivery_id for item in await lease(h)} == {
        ids["boundary"],
        ids["ready"],
    }
    assert (await delivery(h, ids["expired"])).status == DeliveryStatus.SKIPPED
    for state in ("future", "broken", "banned"):
        assert (await delivery(h, ids[state])).status == DeliveryStatus.PENDING


async def test_pending_limit(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    ids = [await emit(h, user) for _ in range(5)]
    first, second, third = [await lease(h, limit=2) for _ in range(3)]
    assert [len(page) for page in (first, second, third)] == [2, 2, 1]
    received = [str(item.delivery_id) for page in (first, second, third) for item in page]
    assert received == [str(id) for id in sorted(ids)]


async def test_concurrent_leases_are_disjoint(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    ids = {await emit(h, user) for _ in range(10)}
    async with h.db() as first, h.db() as second:
        a, b = await asyncio.gather(
            service.lease_deliveries(first, h.now, 5),
            service.lease_deliveries(second, h.now, 5),
        )
        a_ids = {item.delivery_id for item in a}
        b_ids = {item.delivery_id for item in b}
        assert len(a_ids) == len(b_ids) == 5
        assert a_ids.isdisjoint(b_ids)
        assert a_ids | b_ids == ids


async def test_ack_sent_duplicates_and_unknown(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    id = await emit(h, user)
    await ack(
        h,
        DeliveryAckResult(delivery_id=id, status=DeliveryStatus.FAILED, error="temporary"),
        DeliveryAckResult(delivery_id=id, status=DeliveryStatus.SENT, external_id="message-42"),
        DeliveryAckResult(delivery_id=id, status=DeliveryStatus.SENT, external_id="duplicate"),
        DeliveryAckResult(delivery_id=id, status=DeliveryStatus.FAILED, error="late"),
        DeliveryAckResult(delivery_id=id, status="permanent_failure", error="late"),
        DeliveryAckResult(delivery_id=uuid4(), status=DeliveryStatus.SENT),
    )
    await ack(h)
    row = await delivery(h, id)
    assert row.status == DeliveryStatus.SENT
    assert row.external_id == "message-42"
    assert row.last_error is None
    assert row.attempts == 1
    async with h.db() as session:
        link = await session.get(models.TelegramLink, user.id)
        assert link is not None and link.broken_at is None


async def test_ack_exponential_backoff_and_attempt_limit(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    id = await emit(h, user)
    for attempt in range(1, 9):
        await ack(
            h,
            DeliveryAckResult(
                delivery_id=id, status=DeliveryStatus.FAILED, error=f"error-{attempt}"
            ),
        )
        row = await delivery(h, id)
        assert row.attempts == attempt
        assert row.last_error == f"error-{attempt}"
        if attempt < 8:
            delay = 30 * 2 ** (attempt - 1)
            assert row.status == DeliveryStatus.PENDING
            assert row.next_attempt_at == h.now + timedelta(seconds=delay)
            assert await lease(h) == []
            h.advance(timedelta(seconds=delay))
            assert [item.delivery_id for item in await lease(h)] == [id]
        else:
            assert row.status == DeliveryStatus.FAILED
            h.advance(timedelta(hours=2))
            assert await lease(h) == []


async def test_ack_backoff_cap(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    monkeypatch.setattr(service, "MAX_ATTEMPTS", 12)
    user = await h.user()
    await h.link_telegram(user)
    id = await emit(h, user)
    for attempt in range(1, 11):
        await ack(h, DeliveryAckResult(delivery_id=id, status=DeliveryStatus.FAILED))
        row = await delivery(h, id)
        assert row.next_attempt_at == h.now + timedelta(seconds=min(30 * 2 ** (attempt - 1), 3600))
        assert row.status == DeliveryStatus.PENDING


@pytest.mark.parametrize("error", ["bot blocked", None])
async def test_permanent_failure_and_recovery(harness: Harness, error: str | None) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    ids = [await emit(h, user) for _ in range(3)]
    other = await h.user()
    await h.link_telegram(other)
    other_id = await emit(h, other)
    now = h.now
    await ack(h, DeliveryAckResult(delivery_id=ids[0], status="permanent_failure", error=error))
    row = await delivery(h, ids[0])
    assert row.status == DeliveryStatus.FAILED
    assert row.attempts == 1
    assert row.last_error == error
    assert (await delivery(h, ids[1])).status == DeliveryStatus.SKIPPED
    assert (await delivery(h, ids[2])).status == DeliveryStatus.SKIPPED
    assert (await delivery(h, other_id)).status == DeliveryStatus.PENDING
    h.advance(timedelta(seconds=1))
    await ack(h, DeliveryAckResult(delivery_id=ids[1], status="permanent_failure", error="again"))
    assert (await delivery(h, ids[1])).status == DeliveryStatus.FAILED
    async with h.db() as session:
        link = await session.get(models.TelegramLink, user.id)
        assert link is not None and link.broken_at == now
        broken = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user.id,
                    models.Notification.type == NotificationType.TELEGRAM_LINK_BROKEN,
                )
            )
        )
        assert len(broken) == 1
        item = service.notification_view(broken[0])
        assert item.payload.type == NotificationType.TELEGRAM_LINK_BROKEN
        assert item.payload.broken_at == now
        assert item.payload.reason == (error or "Telegram chat is unavailable")
        assert item.payload.link == "/settings"
        assert item.allowed_actions == ["read"]
        assert await session.get(models.NotificationDelivery, (broken[0].id, "telegram")) is None
    no_delivery_id = await emit(h, user)
    async with h.db() as session:
        assert await session.get(models.NotificationDelivery, (no_delivery_id, "telegram")) is None
    async with await h.client(user) as client:
        inbox = await client.get(INBOX)
        assert sum(item["type"] == "telegram_link_broken" for item in inbox.json()["items"]) == 1
    assert {item.delivery_id for item in await lease(h)} == {other_id}


@pytest.mark.parametrize(
    "fixture",
    sorted(path for path in FIXTURES.glob("*.json") if path.stem != "telegram_link_broken"),
    ids=lambda path: path.stem,
)
async def test_payload_fixture_compatibility(harness: Harness, fixture: Path) -> None:
    h = harness
    expected = json.loads(await asyncio.to_thread(fixture.read_text))
    notification = Notification.model_validate(expected)
    user = await h.user()
    await h.link_telegram(user)
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=user.id, type=notification.type, web=True, telegram=True
            )
        )
    await emit(h, user, notification.payload)
    items = await lease(h)
    assert len(items) == 1
    pending = items[0]
    assert pending.model_dump(mode="json")["payload"] == expected["payload"]
