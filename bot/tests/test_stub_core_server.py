import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID

import pytest
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application, Request
from hmm_bot.stub_core import STATE, LinkCode, create_app
from hmm_contracts.common import DeliveryStatus, Problem
from hmm_contracts.fixtures import load_notifications
from hmm_contracts.notifications import (
    ACTION_ROUTES,
    ActionKey,
    NotificationActionResult,
    PendingDelivery,
    TelegramLinkResult,
)

PREFIX = "/api/v1/internal"
HEADERS = {"Authorization": "Bearer stub-secret"}
type StubClient = TestClient[Request, Application]


@dataclass
class Clock:
    value: datetime = datetime(2026, 11, 13, 10, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(clock: Clock) -> AsyncIterator[StubClient]:
    app = create_app("stub-secret", clock=clock)
    async with TestClient(TestServer(app), headers=HEADERS) as client:
        yield client


async def poll(client: StubClient, *, limit: int = 100) -> list[PendingDelivery]:
    response = await client.get(
        f"{PREFIX}/notifications/pending", params={"channel": "telegram", "limit": limit, "wait": 0}
    )
    assert response.status == 200
    return [PendingDelivery.model_validate(item) for item in await response.json()]


async def assert_problem(response: ClientResponse, status: int, code: str) -> Problem:
    assert response.status == status
    assert response.content_type == "application/problem+json"
    problem = Problem.model_validate(await response.json())
    assert problem.status == status
    assert problem.code == code
    assert problem.type == f"http://localhost:8081/errors/{code}"
    return problem


async def link_user(
    client: StubClient, *, telegram_id: int = 42, code: str = "AAAAAAAA"
) -> ClientResponse:
    return await client.post(
        f"{PREFIX}/telegram/link",
        json={
            "code": code,
            "telegram_user_id": telegram_id,
            "chat_id": telegram_id,
            "username": None,
        },
    )


def test_stub_requires_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BOT_SERVICE_TOKEN"):
        create_app()
    monkeypatch.setenv("BOT_SERVICE_TOKEN", "from-environment")
    assert create_app()[STATE].token == "from-environment"
    with pytest.raises(ValueError, match="BOT_SERVICE_TOKEN"):
        create_app(" ")


@pytest.mark.parametrize("token", [None, "", "Bearer wrong", "Bearer parser-token", "stub-secret"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{PREFIX}/notifications/pending?wait=0"),
        ("POST", f"{PREFIX}/notifications/ack"),
        ("POST", f"{PREFIX}/telegram/link"),
        ("POST", f"{PREFIX}/telegram/unlink"),
        ("POST", next(iter(ACTION_ROUTES.values())).path.replace("{id}", str(UUID(int=1)))),
    ],
)
async def test_stub_rejects_service_tokens(token: str | None, method: str, path: str) -> None:
    async with TestClient(TestServer(create_app("stub-secret"))) as client:
        headers = {} if token is None else {"Authorization": token}
        response = await client.request(method, path, headers=headers, json={})
        await assert_problem(response, 401, "unauthenticated")


async def test_stub_rejects_mixed_auth(client: StubClient) -> None:
    response = await client.get(
        f"{PREFIX}/notifications/pending?wait=0", headers={"Cookie": "hmm_session=session"}
    )
    await assert_problem(response, 400, "bad_request")


async def test_stub_fixture_deliveries(client: StubClient) -> None:
    items = await poll(client)
    expected = {
        item.id: item
        for item in load_notifications().values()
        if item.type != "telegram_link_broken"
    }
    assert len(items) == len(expected)
    assert len({item.delivery_id for item in items}) == len(items)
    assert {item.notification_id for item in items} == set(expected)
    for item in items:
        fixture = expected[item.notification_id]
        assert item.type == fixture.type
        assert item.payload == fixture.payload
        assert item.locale == "en"
        assert item.telegram_chat_id == 10001


async def test_stub_lease_expiry(client: StubClient, clock: Clock) -> None:
    first = await poll(client)
    assert await poll(client) == []
    clock.advance(59)
    assert await poll(client) == []
    clock.advance(1)
    assert await poll(client) == first


async def test_stub_concurrent_polls_do_not_double_lease(client: StubClient) -> None:
    first, second = await asyncio.gather(poll(client, limit=40), poll(client, limit=40))
    assert {item.delivery_id for item in first}.isdisjoint(item.delivery_id for item in second)
    assert len(first) + len(second) == len(client.app[STATE].deliveries)


async def test_stub_ack_sent_is_idempotent(client: StubClient, clock: Clock) -> None:
    items = await poll(client)
    delivery_id = items[0].delivery_id
    body = {"results": [{"delivery_id": str(delivery_id), "status": "sent", "external_id": "123"}]}
    for _ in range(2):
        response = await client.post(f"{PREFIX}/notifications/ack", json=body)
        assert response.status == 200
    clock.advance(60)
    assert delivery_id not in {item.delivery_id for item in await poll(client)}
    queued = client.app[STATE].deliveries[delivery_id]
    assert queued.status == DeliveryStatus.SENT
    assert queued.external_id == "123"
    assert queued.attempts == 0


async def test_stub_failed_backoff_and_attempt_limit(client: StubClient, clock: Clock) -> None:
    state = client.app[STATE]
    queued = next(iter(state.deliveries.values()))
    state.deliveries = {queued.delivery.delivery_id: queued}
    for attempt in range(1, 9):
        assert len(await poll(client)) == 1
        response = await client.post(
            f"{PREFIX}/notifications/ack",
            json={
                "results": [
                    {
                        "delivery_id": str(queued.delivery.delivery_id),
                        "status": "failed",
                        "error": "Retry",
                    }
                ]
            },
        )
        assert response.status == 200
        assert queued.attempts == attempt
        delay = min(3600, 30 * 2 ** (attempt - 1))
        assert queued.next_attempt_at == clock() + timedelta(seconds=delay)
        assert await poll(client) == []
        clock.advance(delay)
    assert await poll(client) == []
    assert queued.status == DeliveryStatus.FAILED


async def test_stub_permanent_failure_stops_chat(client: StubClient, clock: Clock) -> None:
    assert (await link_user(client)).status == 200
    items = await poll(client)
    response = await client.post(
        f"{PREFIX}/notifications/ack",
        json={
            "results": [
                {
                    "delivery_id": str(items[0].delivery_id),
                    "status": "permanent_failure",
                    "error": "Blocked",
                }
            ]
        },
    )
    assert response.status == 200
    clock.advance(60)
    assert await poll(client) == []
    state = client.app[STATE]
    assert 42 in state.broken_chats
    assert len(state.web_notifications) == 1
    assert state.web_notifications[0].type == "telegram_link_broken"
    route = next(iter(ACTION_ROUTES.values()))
    response = await client.post(
        route.path.replace("{id}", str(UUID(int=1))),
        headers={"X-Telegram-User-Id": "42"},
        json=route.body,
    )
    assert response.status == 200
    assert 42 not in state.broken_chats


async def test_stub_seven_day_expiry(client: StubClient, clock: Clock) -> None:
    clock.advance(7 * 24 * 60 * 60)
    assert await poll(client) == []
    assert all(
        item.status == DeliveryStatus.SKIPPED for item in client.app[STATE].deliveries.values()
    )


async def test_stub_empty_long_poll_waits(client: StubClient) -> None:
    client.app[STATE].deliveries.clear()
    start = monotonic()
    response = await client.get(f"{PREFIX}/notifications/pending?wait=1")
    assert response.status == 200
    assert await response.json() == []
    assert monotonic() - start >= 0.9


async def test_stub_long_poll_wakes_on_lease_expiry() -> None:
    app = create_app("stub-secret")
    state = app[STATE]
    queued = next(iter(state.deliveries.values()))
    state.deliveries = {queued.delivery.delivery_id: queued}
    queued.lease_until = state.clock() + timedelta(seconds=1)
    async with TestClient(TestServer(app), headers=HEADERS) as client:
        response = await client.get(f"{PREFIX}/notifications/pending?wait=2")
        assert response.status == 200
        assert [item["delivery_id"] for item in await response.json()] == [
            str(queued.delivery.delivery_id)
        ]


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=101", "wait=26", "wait=-1", "channel=web", "limit=no"]
)
async def test_stub_query_validation(client: StubClient, query: str) -> None:
    response = await client.get(f"{PREFIX}/notifications/pending?{query}")
    problem = await assert_problem(response, 422, "validation_error")
    assert problem.errors


@pytest.mark.parametrize(
    ("code", "status", "error"),
    [("unknown", 404, "code_invalid"), ("EXPIRED2", 410, "code_expired")],
)
async def test_stub_link_errors(client: StubClient, code: str, status: int, error: str) -> None:
    await assert_problem(await link_user(client, code=code), status, error)


async def test_stub_link_conflict_and_code_consumption(client: StubClient) -> None:
    response = await link_user(client)
    assert response.status == 200
    user = TelegramLinkResult.model_validate(await response.json())
    assert user.display_name == "Fixture member"
    await assert_problem(await link_user(client), 404, "code_invalid")
    await assert_problem(await link_user(client, code="BBBBBBBB"), 409, "telegram_already_linked")
    assert "BBBBBBBB" in client.app[STATE].codes
    assert all(
        item.delivery.telegram_chat_id == 42 for item in client.app[STATE].deliveries.values()
    )


async def test_stub_link_replaces_previous_telegram(client: StubClient, clock: Clock) -> None:
    response = await link_user(client)
    user = TelegramLinkResult.model_validate(await response.json())
    state = client.app[STATE]
    state.codes["CCCCCCCC"] = LinkCode(user, clock() + timedelta(minutes=10))
    assert (await link_user(client, telegram_id=43, code="CCCCCCCC")).status == 200
    assert 42 not in state.links
    assert state.links[43] == user


async def test_stub_unlink_unknown_is_idempotent(client: StubClient) -> None:
    assert (await link_user(client)).status == 200
    for _ in range(2):
        response = await client.post(f"{PREFIX}/telegram/unlink", json={"telegram_user_id": 42})
        assert response.status == 200
    assert 42 not in client.app[STATE].links
    assert await poll(client) == []


@pytest.mark.parametrize("key", ACTION_ROUTES)
async def test_stub_every_action_route(client: StubClient, key: ActionKey) -> None:
    assert (await link_user(client)).status == 200
    route = ACTION_ROUTES[key]
    entity_id = UUID("019a71c0-0000-7000-8000-000000000001")
    response = await client.request(
        route.method,
        route.path.replace("{id}", str(entity_id)),
        headers={"X-Telegram-User-Id": "42"},
        json=route.body,
    )
    assert response.status == 200
    result = NotificationActionResult.model_validate(await response.json())
    assert result.key == key
    assert result.entity_id == entity_id
    assert result.success
    assert client.app[STATE].actions == [result]


@pytest.mark.parametrize("identity", [None, "", "bad-id", "999"])
async def test_stub_action_requires_linked_identity(
    client: StubClient, identity: str | None
) -> None:
    route = next(iter(ACTION_ROUTES.values()))
    headers = {} if identity is None else {"X-Telegram-User-Id": identity}
    response = await client.post(route.path.replace("{id}", str(UUID(int=1))), headers=headers)
    await assert_problem(response, 401, "telegram_not_linked")


async def test_stub_unknown_ack_is_atomic(client: StubClient) -> None:
    queued = next(iter(client.app[STATE].deliveries.values()))
    response = await client.post(
        f"{PREFIX}/notifications/ack",
        json={
            "results": [
                {"delivery_id": str(queued.delivery.delivery_id), "status": "sent"},
                {"delivery_id": str(UUID(int=0)), "status": "sent"},
            ]
        },
    )
    await assert_problem(response, 404, "not_found")
    assert queued.status == DeliveryStatus.PENDING


@pytest.mark.parametrize("path", ["telegram/link", "telegram/unlink", "notifications/ack"])
async def test_stub_invalid_body(client: StubClient, path: str) -> None:
    for data in ("{", "{}", "[]"):
        response = await client.post(f"{PREFIX}/{path}", data=data)
        problem = await assert_problem(response, 422, "validation_error")
        assert problem.errors


async def test_stub_unknown_routes_are_problems(client: StubClient) -> None:
    response = await client.get(f"{PREFIX}/unknown")
    await assert_problem(response, 404, "not_found")
    response = await client.get(f"{PREFIX}/telegram/link")
    await assert_problem(response, 405, "method_not_allowed")
