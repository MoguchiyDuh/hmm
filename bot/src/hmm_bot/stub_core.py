import argparse
import asyncio
import hmac
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiohttp import web
from hmm_contracts.common import (
    Contract,
    DeliveryStatus,
    ErrorCode,
    Locale,
    Problem,
    ValidationIssue,
)
from hmm_contracts.fixtures import load_notifications
from hmm_contracts.notifications import (
    ACTION_ROUTES,
    ActionKey,
    DeliveryAck,
    Notification,
    NotificationActionResult,
    NotificationType,
    PendingDeliveriesQuery,
    PendingDelivery,
    TelegramLinkBrokenPayload,
    TelegramLinkRequest,
    TelegramLinkResult,
    TelegramUnlinkRequest,
)
from pydantic import ValidationError


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class QueuedDelivery:
    delivery: PendingDelivery
    next_attempt_at: datetime
    user_id: UUID
    lease_until: datetime | None = None
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    external_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LinkCode:
    user: TelegramLinkResult
    expires_at: datetime


class StubState:
    def __init__(
        self, token: str, public_url: str, clock: Callable[[], datetime], chat_id: int
    ) -> None:
        self.token = token
        self.public_url = public_url
        self.clock = clock
        self.changed = asyncio.Condition()
        self.deliveries: dict[UUID, QueuedDelivery] = {}
        self.links: dict[int, TelegramLinkResult] = {}
        self.chat_ids: dict[int, int] = {}
        self.codes: dict[str, LinkCode] = {}
        self.broken_chats: set[int] = set()
        self.actions: list[NotificationActionResult] = []
        self.web_notifications: list[Notification] = []
        now = clock()
        notifications = load_notifications()
        for notification in notifications.values():
            if notification.type == NotificationType.TELEGRAM_LINK_BROKEN:
                continue
            delivery = PendingDelivery(
                delivery_id=UUID(int=notification.id.int ^ (1 << 48)),
                notification_id=notification.id,
                telegram_chat_id=chat_id,
                locale=Locale.EN,
                type=notification.type,
                payload=notification.payload,
                created_at=now,
            )
            self.deliveries[delivery.delivery_id] = QueuedDelivery(
                delivery, now, notification.user_id
            )
        user_id = next(iter(notifications.values())).user_id
        user = TelegramLinkResult(user_id=user_id, display_name="Fixture member")
        self.codes["AAAAAAAA"] = LinkCode(user, now + timedelta(minutes=10))
        self.codes["EXPIRED2"] = LinkCode(user, now - timedelta(seconds=1))
        other = TelegramLinkResult(user_id=UUID(int=user_id.int + 1), display_name="Other member")
        self.codes["BBBBBBBB"] = LinkCode(other, now + timedelta(minutes=10))

    def ready_at(self, queued: QueuedDelivery) -> datetime | None:
        if (
            queued.status not in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
            or queued.attempts >= 8
        ):
            return None
        if queued.delivery.telegram_chat_id in self.broken_chats:
            return None
        if self.clock() >= queued.delivery.created_at + timedelta(days=7):
            queued.status = DeliveryStatus.SKIPPED
            return None
        return max(queued.next_attempt_at, queued.lease_until or queued.next_attempt_at)


STATE = web.AppKey("stub_state", StubState)


class StubProblem(Exception):
    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        errors: list[ValidationIssue] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.errors = errors


@web.middleware
async def authenticate(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    state = request.app[STATE]
    try:
        if "hmm_session" in request.cookies and (
            "Authorization" in request.headers or "X-Telegram-User-Id" in request.headers
        ):
            raise StubProblem(
                ErrorCode.BAD_REQUEST, "Session cookies cannot be mixed with bot credentials"
            )
        credentials = request.headers.get("Authorization", "")
        expected = f"Bearer {state.token}"
        if not hmac.compare_digest(credentials.encode(), expected.encode()):
            raise StubProblem(ErrorCode.UNAUTHENTICATED)
        telegram_id: int | None = None
        if not request.path.startswith("/api/v1/internal/"):
            identity = request.headers.get("X-Telegram-User-Id", "")
            try:
                telegram_id = int(identity)
            except ValueError as exc:
                raise StubProblem(ErrorCode.TELEGRAM_NOT_LINKED) from exc
            if telegram_id not in state.links:
                raise StubProblem(ErrorCode.TELEGRAM_NOT_LINKED)
        response = await handler(request)
        if telegram_id is not None and response.status < 400:
            state.broken_chats.discard(state.chat_ids[telegram_id])
        return response
    except ValidationError as exc:
        errors = [
            ValidationIssue(loc=list(error["loc"]), msg=error["msg"]) for error in exc.errors()
        ]
        problem = Problem.build(
            public_url=state.public_url, code=ErrorCode.VALIDATION_ERROR, errors=errors
        )
    except StubProblem as exc:
        problem = Problem.build(
            public_url=state.public_url, code=exc.code, detail=exc.detail, errors=exc.errors
        )
    except web.HTTPException as exc:
        code = {
            400: ErrorCode.BAD_REQUEST,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.METHOD_NOT_ALLOWED,
            413: ErrorCode.FILE_TOO_LARGE,
            415: ErrorCode.UNSUPPORTED_FILE_TYPE,
        }.get(exc.status, ErrorCode.INTERNAL_ERROR)
        problem = Problem.build(public_url=state.public_url, code=code)
    return web.json_response(
        problem.model_dump(mode="json"),
        status=problem.status,
        content_type="application/problem+json",
    )


async def _body[T: Contract](request: web.Request, model: type[T]) -> T:
    return model.model_validate_json(await request.read())


async def pending(request: web.Request) -> web.Response:
    query = PendingDeliveriesQuery.model_validate(dict(request.query))
    state = request.app[STATE]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + query.wait
    async with state.changed:
        while True:
            now = state.clock()
            items: list[PendingDelivery] = []
            upcoming: list[datetime] = []
            for queued in state.deliveries.values():
                ready = state.ready_at(queued)
                if ready is None:
                    continue
                upcoming.append(ready)
                if ready <= now and len(items) < query.limit:
                    queued.lease_until = now + timedelta(seconds=60)
                    items.append(queued.delivery)
            if items or loop.time() >= deadline:
                return web.json_response([item.model_dump(mode="json") for item in items])
            delay = deadline - loop.time()
            if upcoming:
                delay = min(delay, max(0.0, (min(upcoming) - now).total_seconds()))
            try:
                async with asyncio.timeout(delay):
                    await state.changed.wait()
            except TimeoutError:
                continue


async def ack(request: web.Request) -> web.Response:
    body = await _body(request, DeliveryAck)
    state = request.app[STATE]
    async with state.changed:
        if any(result.delivery_id not in state.deliveries for result in body.results):
            raise StubProblem(ErrorCode.NOT_FOUND, "Unknown delivery")
        for result in body.results:
            queued = state.deliveries[result.delivery_id]
            if queued.status in (DeliveryStatus.SENT, DeliveryStatus.SKIPPED):
                continue
            queued.lease_until = None
            queued.external_id = result.external_id
            queued.error = result.error
            if result.status == "permanent_failure":
                chat_id = queued.delivery.telegram_chat_id
                state.broken_chats.add(chat_id)
                state.web_notifications.append(
                    Notification(
                        id=UUID(int=queued.delivery.notification_id.int ^ (1 << 47)),
                        user_id=queued.user_id,
                        type=NotificationType.TELEGRAM_LINK_BROKEN,
                        payload=TelegramLinkBrokenPayload(
                            link="/settings#telegram",
                            broken_at=state.clock(),
                            reason=result.error or "Telegram delivery permanently failed",
                        ),
                        created_at=state.clock(),
                    )
                )
                for other in state.deliveries.values():
                    if (
                        other.delivery.telegram_chat_id == chat_id
                        and other.status != DeliveryStatus.SENT
                    ):
                        other.status = DeliveryStatus.SKIPPED
            elif result.status == DeliveryStatus.SENT:
                queued.status = DeliveryStatus.SENT
            else:
                queued.attempts += 1
                queued.status = DeliveryStatus.FAILED
                queued.next_attempt_at = state.clock() + timedelta(
                    seconds=min(3600, 30 * 2 ** (queued.attempts - 1))
                )
        state.changed.notify_all()
    return web.json_response({"success": True})


async def link(request: web.Request) -> web.Response:
    body = await _body(request, TelegramLinkRequest)
    state = request.app[STATE]
    code = state.codes.get(body.code)
    if code is None:
        raise StubProblem(ErrorCode.CODE_INVALID)
    if code.expires_at <= state.clock():
        raise StubProblem(ErrorCode.CODE_EXPIRED)
    current = state.links.get(body.telegram_user_id)
    if current is not None and current.user_id != code.user.user_id:
        raise StubProblem(ErrorCode.TELEGRAM_ALREADY_LINKED)
    for telegram_id, user in list(state.links.items()):
        if user.user_id == code.user.user_id:
            del state.links[telegram_id]
            del state.chat_ids[telegram_id]
    state.links[body.telegram_user_id] = code.user
    state.chat_ids[body.telegram_user_id] = body.chat_id
    state.broken_chats.discard(body.chat_id)
    for queued in state.deliveries.values():
        if queued.user_id == code.user.user_id:
            queued.delivery = queued.delivery.model_copy(update={"telegram_chat_id": body.chat_id})
    del state.codes[body.code]
    return web.json_response(code.user.model_dump(mode="json"))


async def unlink(request: web.Request) -> web.Response:
    body = await _body(request, TelegramUnlinkRequest)
    state = request.app[STATE]
    user = state.links.pop(body.telegram_user_id, None)
    state.chat_ids.pop(body.telegram_user_id, None)
    if user is not None:
        for queued in state.deliveries.values():
            if queued.user_id == user.user_id and queued.status != DeliveryStatus.SENT:
                queued.status = DeliveryStatus.SKIPPED
    return web.json_response({"success": True})


async def action(request: web.Request) -> web.Response:
    key = ActionKey(request.match_info.route.name)
    route = ACTION_ROUTES[key]
    if route.body is not None:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise StubProblem(ErrorCode.BAD_REQUEST, "Invalid JSON body") from exc
        if body != route.body:
            raise StubProblem(
                ErrorCode.VALIDATION_ERROR,
                errors=[
                    ValidationIssue(loc=["body"], msg="Action body does not match the contract")
                ],
            )
    try:
        entity_id = UUID(request.match_info["id"])
    except ValueError as exc:
        raise StubProblem(
            ErrorCode.VALIDATION_ERROR,
            errors=[ValidationIssue(loc=["path", "id"], msg="Invalid entity UUID")],
        ) from exc
    result = NotificationActionResult(
        key=key, entity_id=entity_id, success=True, message="Stub action accepted"
    )
    request.app[STATE].actions.append(result)
    return web.json_response(result.model_dump(mode="json"))


def create_app(
    token: str | None = None,
    *,
    public_url: str = "http://localhost:8081",
    clock: Callable[[], datetime] = _now,
    chat_id: int = 10001,
) -> web.Application:
    token = token if token is not None else os.environ.get("BOT_SERVICE_TOKEN", "")
    if not token.strip():
        raise ValueError("BOT_SERVICE_TOKEN must be configured")
    app = web.Application(middlewares=[authenticate])
    app[STATE] = StubState(token, public_url, clock, chat_id)
    app.router.add_get("/api/v1/internal/notifications/pending", pending)
    app.router.add_post("/api/v1/internal/notifications/ack", ack)
    app.router.add_post("/api/v1/internal/telegram/link", link)
    app.router.add_post("/api/v1/internal/telegram/unlink", unlink)
    for key, route in ACTION_ROUTES.items():
        app.router.add_route(route.method, route.path, action, name=key.value)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8081, type=int)
    parser.add_argument("--chat-id", default=10001, type=int)
    args = parser.parse_args()
    try:
        app = create_app(
            public_url=os.environ.get("PUBLIC_URL", f"http://{args.host}:{args.port}"),
            chat_id=args.chat_id,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        "Stub link codes: AAAAAAAA (valid), BBBBBBBB (other user), EXPIRED2 (expired)", flush=True
    )
    web.run_app(app, host=args.host, port=args.port)
