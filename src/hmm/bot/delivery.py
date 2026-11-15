import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from uuid import UUID

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.bot.runtime import BotRuntime
from hmm.bot.templates import Rendered, render
from hmm.common import DeliveryStatus
from hmm.notifications.payloads import DeliveryAckResult, PendingDelivery
from hmm.notifications.service import acknowledge, lease_deliveries
from hmm.platform.db import transaction

BATCH = 50
IDLE_SECONDS = 2.0
LEASE_BUDGET_SECONDS = 45.0
GLOBAL_INTERVAL = 1 / 25
CHAT_INTERVAL = 1.0
RETRY_AFTER_ATTEMPTS = 3
LRU_SIZE = 10_000
PERMANENT_ERRORS = ("chat not found", "user is deactivated", "bot was blocked")

log = logging.getLogger("hmm.bot.delivery")

type Send = Callable[[int, Rendered], Awaitable[int]]
type Sleep = Callable[[float], Awaitable[None]]


class Throttle:
    def __init__(
        self,
        *,
        global_interval: float = GLOBAL_INTERVAL,
        chat_interval: float = CHAT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.global_interval = global_interval
        self.chat_interval = chat_interval
        self.clock = clock
        self.sleep = sleep
        self.next_global = 0.0
        self.next_chat: dict[int, float] = {}

    async def wait(self, chat_id: int) -> None:
        now = self.clock()
        ready = max(self.next_global, self.next_chat.get(chat_id, 0.0))
        if ready > now:
            await self.sleep(ready - now)
            now = ready
        self.next_global = now + self.global_interval
        self.next_chat[chat_id] = now + self.chat_interval
        if len(self.next_chat) > LRU_SIZE:
            self.next_chat = {chat: at for chat, at in self.next_chat.items() if at > now}


async def chat_timezones(session: AsyncSession, chat_ids: set[int]) -> dict[int, str]:
    if not chat_ids:
        return {}
    rows = await session.execute(
        select(models.TelegramLink.chat_id, models.User.timezone)
        .join(models.User, models.User.id == models.TelegramLink.user_id)
        .where(models.TelegramLink.chat_id.in_(chat_ids), models.User.timezone.is_not(None))
    )
    return {chat_id: timezone for chat_id, timezone in rows if timezone}


def ack(
    delivery: PendingDelivery,
    status: DeliveryStatus | str,
    *,
    external_id: str | None = None,
    error: str | None = None,
) -> DeliveryAckResult:
    return DeliveryAckResult.model_validate(
        {
            "delivery_id": delivery.delivery_id,
            "status": status,
            "external_id": external_id,
            "error": error,
        }
    )


class Deliverer:
    def __init__(
        self,
        runtime: BotRuntime,
        send: Send,
        *,
        throttle: Throttle | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self.send = send
        self.throttle = throttle or Throttle()
        self.sleep = sleep
        self.sent: OrderedDict[UUID, str] = OrderedDict()

    def remember(self, notification_id: UUID, external_id: str) -> None:
        self.sent[notification_id] = external_id
        self.sent.move_to_end(notification_id)
        while len(self.sent) > LRU_SIZE:
            self.sent.popitem(last=False)

    async def deliver(
        self, delivery: PendingDelivery, timezone: str | None = None
    ) -> DeliveryAckResult:
        known = self.sent.get(delivery.notification_id)
        if known is not None:
            return ack(delivery, DeliveryStatus.SENT, external_id=known)
        try:
            rendered = render(
                delivery.payload,
                self.runtime.config.public_url,
                locale=delivery.locale,
                now=self.runtime.clock.now(),
                timezone=timezone,
            )
        except Exception as error:
            log.exception("Rendering %s failed", delivery.notification_id)
            return ack(delivery, DeliveryStatus.FAILED, error=f"render: {error}")
        for attempt in range(RETRY_AFTER_ATTEMPTS + 1):
            await self.throttle.wait(delivery.telegram_chat_id)
            try:
                message_id = await self.send(delivery.telegram_chat_id, rendered)
            except TelegramRetryAfter as error:
                if attempt == RETRY_AFTER_ATTEMPTS:
                    return ack(delivery, DeliveryStatus.FAILED, error=error.message)
                await self.sleep(error.retry_after)
                continue
            except TelegramForbiddenError as error:
                return ack(delivery, "permanent_failure", error=error.message)
            except TelegramBadRequest as error:
                permanent = any(text in error.message.lower() for text in PERMANENT_ERRORS)
                status = "permanent_failure" if permanent else DeliveryStatus.FAILED
                return ack(delivery, status, error=error.message)
            except TelegramAPIError as error:
                return ack(delivery, DeliveryStatus.FAILED, error=error.message)
            self.remember(delivery.notification_id, str(message_id))
            return ack(delivery, DeliveryStatus.SENT, external_id=str(message_id))
        raise AssertionError("unreachable")

    async def tick(self) -> int:
        sessionmaker = self.runtime.sessionmaker
        async with transaction(sessionmaker) as session:
            deliveries = await lease_deliveries(session, self.runtime.clock.now(), BATCH)
            zones = await chat_timezones(session, {item.telegram_chat_id for item in deliveries})
        started = time.monotonic()
        for delivery in deliveries:
            if time.monotonic() - started > LEASE_BUDGET_SECONDS:
                break
            result = await self.deliver(delivery, zones.get(delivery.telegram_chat_id))
            async with transaction(sessionmaker) as session:
                await acknowledge(session, self.runtime.clock.now(), [result])
        return len(deliveries)

    async def run(self) -> None:
        while True:
            try:
                count = await self.tick()
            except Exception:
                log.exception("Delivery tick failed")
                count = 0
            if count < BATCH:
                await self.sleep(IDLE_SECONDS)
