from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from factories import formed, make_event, stored_member
from harness import Harness
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.bot import commands
from hmm.bot.callbacks import (
    ActionCallback,
    AskAreaCallback,
    CancelCallback,
    EventCallback,
    EventOp,
    LanguageCallback,
    MenuItem,
)
from hmm.bot.delivery import Deliverer, Throttle
from hmm.bot.fmt import TEXTS
from hmm.bot.i18n import LINK_PATH
from hmm.bot.runtime import (
    Body,
    BotRuntime,
    Reply,
    respond,
    respond_anonymous,
)
from hmm.bot.templates import Rendered
from hmm.common import DeliveryStatus, ErrorCode, Locale, MemberState, NotificationChannel
from hmm.notifications import service
from hmm.notifications.payloads import ActionKey, EventCancelledPayload

METHOD = SendMessage(chat_id=1, text="x")
EN = TEXTS[Locale.EN]
RU = TEXTS[Locale.RU]


async def emit_event_cancelled(h: Harness, user: models.User) -> UUID:
    payload = EventCancelledPayload(
        link="/events", event_id=uuid4(), event_title="Hack", reason="Venue closed"
    )
    async with h.db() as session:
        row = await service.notify(session, user.id, payload, h.now)
        assert row is not None
        return row.id


def bot_runtime(h: Harness) -> BotRuntime:
    return BotRuntime(
        config=h.app.state.config, sessionmaker=h.sessionmaker, jobs=h.jobs, clock=h.clock
    )


class FakeTelegram:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.sent: list[tuple[int, Rendered]] = []
        self.slept: list[float] = []

    async def send(self, chat_id: int, rendered: Rendered) -> int:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, rendered))
        return 1000 + len(self.sent)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def deliverer(self, h: Harness) -> Deliverer:
        return Deliverer(
            bot_runtime(h),
            self.send,
            throttle=Throttle(global_interval=0, chat_interval=0, sleep=self.sleep),
            sleep=self.sleep,
        )


async def delivery_row(h: Harness, notification_id: UUID) -> models.NotificationDelivery:
    async with h.db() as session:
        row = await session.get(
            models.NotificationDelivery, (notification_id, NotificationChannel.TELEGRAM)
        )
        assert row is not None
        return row


async def telegram_link(h: Harness, user: models.User) -> models.TelegramLink | None:
    async with h.db() as session:
        return await session.get(models.TelegramLink, user.id)


def callbacks(reply: Reply) -> list[str]:
    assert reply.markup is not None
    return [
        button.callback_data
        for row in reply.markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


async def test_delivery_sends_and_dedupes(harness: Harness) -> None:
    h = harness
    user = await h.user()
    chat = await h.link_telegram(user)
    first = await emit_event_cancelled(h, user)
    second = await emit_event_cancelled(h, user)
    telegram = FakeTelegram()
    deliverer = telegram.deliverer(h)
    assert await deliverer.tick() == 2
    assert [chat_id for chat_id, _ in telegram.sent] == [chat, chat]
    assert "was cancelled" in telegram.sent[0][1].text
    for notification_id, external_id in ((first, "1001"), (second, "1002")):
        row = await delivery_row(h, notification_id)
        assert row.status == DeliveryStatus.SENT
        assert row.external_id == external_id
    async with h.db() as session:
        await session.execute(
            update(models.NotificationDelivery)
            .where(models.NotificationDelivery.notification_id == first)
            .values(status=DeliveryStatus.PENDING, next_attempt_at=h.now)
        )
    assert await deliverer.tick() == 1
    assert len(telegram.sent) == 2
    row = await delivery_row(h, first)
    assert row.status == DeliveryStatus.SENT
    assert row.external_id == "1001"
    assert await deliverer.tick() == 0


async def test_delivery_retry_after(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    notification_id = await emit_event_cancelled(h, user)
    telegram = FakeTelegram(TelegramRetryAfter(METHOD, "flood", 3))
    await telegram.deliverer(h).tick()
    assert telegram.slept == [3]
    assert (await delivery_row(h, notification_id)).status == DeliveryStatus.SENT


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user"),
        TelegramBadRequest(METHOD, "Bad Request: chat not found"),
    ],
    ids=["forbidden", "chat_not_found"],
)
async def test_delivery_permanent_failure_breaks_link(harness: Harness, error: Exception) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    notification_id = await emit_event_cancelled(h, user)
    await FakeTelegram(error).deliverer(h).tick()
    row = await delivery_row(h, notification_id)
    assert row.status == DeliveryStatus.FAILED
    assert row.last_error is not None
    link = await telegram_link(h, user)
    assert link is not None and link.broken_at == h.now


async def test_delivery_transient_failure_retries(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    notification_id = await emit_event_cancelled(h, user)
    await (
        FakeTelegram(TelegramBadRequest(METHOD, "Bad Request: can't parse entities"))
        .deliverer(h)
        .tick()
    )
    row = await delivery_row(h, notification_id)
    assert row.status == DeliveryStatus.PENDING
    assert row.attempts == 1
    assert row.next_attempt_at > h.now
    link = await telegram_link(h, user)
    assert link is not None and link.broken_at is None


async def add_code(h: Harness, user: models.User, code: str) -> None:
    async with h.db() as session:
        session.add(
            models.TelegramLinkCode(
                code=code, user_id=user.id, expires_at=h.now + timedelta(minutes=10)
            )
        )


async def link_with(
    h: Harness, code: str, telegram_user_id: int, language_code: str | None = None
) -> Reply:
    return await respond_anonymous(
        bot_runtime(h),
        lambda session, now: commands.link_account(
            bot_runtime(h),
            session,
            now,
            code=code,
            telegram_user_id=telegram_user_id,
            chat_id=telegram_user_id,
            username="tg",
            language_code=language_code,
        ),
    )


async def test_linking(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    first = await h.user(display_name="Ada")
    second = await h.user()
    unknown = await respond(runtime, 777, lambda s, a, n: commands.profile(runtime, s, a, n))
    assert not unknown.ok
    assert unknown.text == EN.errors[ErrorCode.UNAUTHENTICATED].format(
        url=runtime.config.public_url
    )
    assert LINK_PATH in unknown.text
    greeting = await respond_anonymous(runtime, lambda s, n: commands.greet(runtime, s, 777))
    assert "link your account" in greeting.text
    assert not (await link_with(h, "missing", 777)).ok
    await add_code(h, first, "first-code")
    linked = await link_with(h, "first-code", 777)
    assert linked.ok
    assert "Ada" in linked.text
    link = await telegram_link(h, first)
    assert link is not None and link.telegram_user_id == 777
    reused = await link_with(h, "first-code", 777)
    assert reused.text == EN.errors[ErrorCode.CODE_EXPIRED].format(url=runtime.config.public_url)
    await add_code(h, second, "second-code")
    taken = await link_with(h, "second-code", 777)
    assert taken.text == EN.errors[ErrorCode.TELEGRAM_ALREADY_LINKED]
    me = await respond(runtime, 777, lambda s, a, n: commands.profile(runtime, s, a, n))
    assert me.ok and "Ada" in me.text
    greeting = await respond_anonymous(runtime, lambda s, n: commands.greet(runtime, s, 777))
    assert "Hi, <b>Ada</b>" in greeting.text
    other = await respond_anonymous(runtime, lambda s, n: commands.unknown(runtime, s, 777))
    assert other.text == EN.unknown
    await respond_anonymous(runtime, lambda s, n: commands.unlink(s, 777))
    assert await telegram_link(h, first) is None


async def test_language(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    user = await h.user(display_name="Ada")
    unlinked = await respond(
        runtime,
        555,
        lambda s, a, n: commands.profile(runtime, s, a, n),
        language_code="ru",
    )
    assert unlinked.text == RU.errors[ErrorCode.UNAUTHENTICATED].format(
        url=runtime.config.public_url
    )
    await add_code(h, user, "ru-code")
    linked = await link_with(h, "ru-code", 555, "ru")
    assert linked.locale is Locale.RU
    assert linked.text.startswith(RU.linked.format(name="<b>Ada</b>"))
    async with h.db() as session:
        stored = await session.get(models.User, user.id)
    assert stored is not None and stored.locale == Locale.RU
    teams = await respond(runtime, 555, lambda s, a, n: commands.teams(runtime, s, a, n))
    assert teams.text == RU.no_teams

    async def menu(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        return commands.language_menu()

    shown = await respond(runtime, 555, menu, member=False)
    assert shown.text == RU.language_prompt
    assert LanguageCallback(locale=Locale.EN).pack() in callbacks(shown)
    switched = await respond(
        runtime,
        555,
        lambda s, a, n: commands.set_language(runtime, s, a, n, Locale.EN),
        member=False,
    )
    assert switched.text == EN.language_set and switched.locale is Locale.EN
    async with h.db() as session:
        stored = await session.get(models.User, user.id)
    assert stored is not None and stored.locale == Locale.EN
    teams = await respond(runtime, 555, lambda s, a, n: commands.teams(runtime, s, a, n))
    assert teams.text == EN.no_teams
    assert [command.command for command in commands.bot_commands(Locale.RU)] == [
        command.name for command in commands.MENU_COMMANDS
    ]


async def test_team_placement_accept(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    admin = await h.admin()
    _, users, team = await formed(h, admin)
    telegram_user_id = await h.link_telegram(users[0])
    reply = await respond(
        runtime, telegram_user_id, lambda s, a, n: commands.teams(runtime, s, a, n)
    )
    assert team.name in reply.text
    accept = ActionCallback(key=ActionKey.PLACEMENT_ACCEPT, entity_id=team.id).pack()
    assert accept in callbacks(reply)
    done = await respond(
        runtime,
        telegram_user_id,
        lambda s, a, n: commands.action(runtime, s, a, n, ActionKey.PLACEMENT_ACCEPT, team.id),
    )
    assert done == Reply(EN.action_done[ActionKey.PLACEMENT_ACCEPT])
    assert (await stored_member(h, team, users[0])).state == MemberState.CONFIRMED
    again = await respond(
        runtime,
        telegram_user_id,
        lambda s, a, n: commands.action(runtime, s, a, n, ActionKey.PLACEMENT_ACCEPT, uuid4()),
    )
    assert not again.ok and not again.retry


async def test_events_and_enroll(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    event = await make_event(h)
    user = await h.user()
    telegram_user_id = await h.link_telegram(user)
    listing = await respond(
        runtime, telegram_user_id, lambda s, a, n: commands.events(runtime, s, a, n)
    )
    assert event.title in listing.text
    assert EventCallback(op=EventOp.SHOW, event_id=event.id).pack() in callbacks(listing)

    async def card(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        event_id = await commands.resolve_event_id(session, event.slug)
        return await commands.event_card(runtime, session, actor, now, event_id)

    shown = await respond(runtime, telegram_user_id, card)
    assert event.title in shown.text
    enrolled = await respond(
        runtime,
        telegram_user_id,
        lambda s, a, n: commands.event_op(runtime, s, a, n, EventOp.ENROLL, event.id),
    )
    assert enrolled.ok and enrolled.text.startswith("✅ You're in the queue.")
    assert event.title in enrolled.text
    back = EventCallback(op=EventOp.LIST, event_id=event.id).pack()
    assert back in callbacks(enrolled)
    listed = await respond(
        runtime,
        telegram_user_id,
        lambda s, a, n: commands.event_op(runtime, s, a, n, EventOp.LIST, event.id),
    )
    assert listed.text == listing.text
    menu = await respond(
        runtime,
        telegram_user_id,
        lambda s, a, n: commands.menu(runtime, s, a, n, MenuItem.EVENTS),
    )
    assert menu.text == listing.text
    async with h.db() as session:
        enrollment = await session.scalar(
            select(models.Enrollment).where(
                models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id
            )
        )
    assert enrollment is not None
    shown = await respond(runtime, telegram_user_id, card)
    assert EventCallback(op=EventOp.WITHDRAW, event_id=event.id).pack() in callbacks(shown)


async def test_ask_flow(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    await h.seed_areas()
    user = await h.user()
    telegram_user_id = await h.link_telegram(user)
    areas = await respond(runtime, telegram_user_id, lambda s, a, n: commands.ask_areas(s))
    assert AskAreaCallback(area_key="backend").pack() in callbacks(areas)
    assert CancelCallback().pack() in callbacks(areas)
    data: dict[str, str] = {}

    async def topic(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        reply, values = await commands.ask_topic(session, "backend")
        data.update(values)
        return reply

    prompted = await respond(runtime, telegram_user_id, topic)
    assert prompted.ok and callbacks(prompted) == [CancelCallback().pack()]
    assert data == {"area_key": "backend"}
    data.clear()

    def start(area_key: str) -> Body:
        async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
            reply, values = await commands.start_ask(session, area_key, "Async Python")
            data.update(values)
            return reply

        return body

    assert not (await respond(runtime, telegram_user_id, start("nope"))).ok
    assert data == {}
    assert (await respond(runtime, telegram_user_id, start("backend"))).ok
    assert data == {"area_key": "backend", "topic": "Async Python"}

    def create(goal: str) -> Body:
        return lambda s, a, n: commands.create_help_request(runtime, s, a, n, goal=goal, **data)

    invalid = await respond(runtime, telegram_user_id, create("   "))
    assert not invalid.ok and invalid.retry
    created = await respond(runtime, telegram_user_id, create("Ship a service with tracing"))
    assert created.ok
    async with h.db() as session:
        request = await session.scalar(
            select(models.HelpRequest).where(models.HelpRequest.learner_id == user.id)
        )
    assert request is not None and request.topic == "Async Python"


async def test_dnd(harness: Harness) -> None:
    h = harness
    runtime = bot_runtime(h)
    user = await h.user()
    telegram_user_id = await h.link_telegram(user)

    async def dnd(argument: str | None) -> Reply:
        return await respond(
            runtime, telegram_user_id, lambda s, a, n: commands.dnd(runtime, s, a, n, argument)
        )

    assert "off" in (await dnd(None)).text
    assert "until" in (await dnd("3")).text
    async with h.db() as session:
        stored = await session.get(models.User, user.id)
    assert stored is not None and stored.dnd_until == h.now + timedelta(days=3)
    assert (await dnd("abc")).text.startswith("Usage: /dnd")
    assert (await dnd("off")).text == "🔔 Do not disturb is off."
