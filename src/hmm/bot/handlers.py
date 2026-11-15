import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from html import unescape
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import KICKED, ChatMemberUpdatedFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommandScopeChat,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.bot import commands
from hmm.bot.callbacks import (
    ActionCallback,
    AskAreaCallback,
    CancelCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    EventCallback,
    EventOp,
    HelpCallback,
    LanguageCallback,
    MenuCallback,
    OutcomeCallback,
)
from hmm.bot.fmt import clip, tr
from hmm.bot.runtime import (
    AnonymousBody,
    Body,
    BotRuntime,
    Reply,
    respond,
    respond_anonymous,
)
from hmm.common import Locale

TOAST_LIMIT = 200
CONFIRMED_MARK = "☑"
PENDING_MARK = "☐"
PROMPT_KEY = "prompt_id"
TAG = re.compile(r"<[^>]+>")


type FlowStart = Callable[[AsyncSession], Awaitable[tuple[Reply, dict[str, str]]]]


class Flow(StatesGroup):
    ask_topic = State()
    ask_goal = State()
    help_message = State()
    checkin_note = State()


async def send(message: Message, reply: Reply) -> Message:
    return await message.answer(reply.text, reply_markup=reply.markup)


async def run(
    message: Message, runtime: BotRuntime, body: Body, *, member: bool = True
) -> tuple[Reply, Message] | None:
    user = message.from_user
    if user is None:
        return None
    reply = await respond(runtime, user.id, body, member=member, language_code=user.language_code)
    return reply, await send(message, reply)


async def anonymous(runtime: BotRuntime, user: User, body: AnonymousBody) -> Reply:
    return await respond_anonymous(
        runtime, body, telegram_user_id=user.id, language_code=user.language_code
    )


def fixed(build: Callable[[], Reply]) -> AnonymousBody:
    async def body(session: AsyncSession, now: datetime) -> Reply:
        return build()

    return body


async def apply_commands(bot: Bot | None, chat_id: int, locale: Locale | None) -> None:
    if bot is None:
        return
    scope = BotCommandScopeChat(chat_id=chat_id)
    with suppress(TelegramAPIError):
        if locale is None:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(commands.bot_commands(locale), scope=scope)


def plain(text: str) -> str:
    return clip(unescape(TAG.sub("", text)), TOAST_LIMIT)


async def edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message:
            raise


async def start_flow(
    state: FSMContext, flow: State, data: dict[str, str], prompt_message: Message
) -> None:
    await state.set_state(flow)
    await state.set_data({**data, PROMPT_KEY: str(prompt_message.message_id)})


async def retire_prompt(message: Message, state: FSMContext) -> None:
    prompt_id = (await state.get_data()).get(PROMPT_KEY)
    if prompt_id is None or message.bot is None:
        return
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=int(prompt_id), reply_markup=None
        )
    except TelegramBadRequest:
        return


async def end_flow(message: Message, state: FSMContext) -> None:
    await retire_prompt(message, state)
    await state.clear()


async def on_start_link(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    user = message.from_user
    if user is None or command.args is None:
        return
    code = command.args.strip()
    reply = await anonymous(
        runtime,
        user,
        lambda session, now: commands.link_account(
            runtime,
            session,
            now,
            code=code,
            telegram_user_id=user.id,
            chat_id=message.chat.id,
            username=user.username,
            language_code=user.language_code,
        ),
    )
    await send(message, reply)
    if reply.ok:
        await apply_commands(message.bot, message.chat.id, reply.locale)


async def on_start(message: Message, runtime: BotRuntime) -> None:
    user = message.from_user
    if user is None:
        return
    await send(
        message,
        await anonymous(
            runtime, user, lambda session, now: commands.greet(runtime, session, user.id)
        ),
    )


async def on_unlink(message: Message, runtime: BotRuntime, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await end_flow(message, state)
    reply = await anonymous(runtime, user, lambda session, now: commands.unlink(session, user.id))
    await send(message, reply)
    if reply.ok:
        await apply_commands(message.bot, message.chat.id, None)


async def reply_fixed(message: Message, runtime: BotRuntime, build: Callable[[], Reply]) -> None:
    if message.from_user is not None:
        await send(message, await anonymous(runtime, message.from_user, fixed(build)))


async def on_help(message: Message, runtime: BotRuntime) -> None:
    await reply_fixed(message, runtime, lambda: Reply(commands.help_text()))


async def on_settings(message: Message, runtime: BotRuntime) -> None:
    await reply_fixed(message, runtime, commands.settings_reply)


async def on_cancel(message: Message, state: FSMContext, runtime: BotRuntime) -> None:
    if await state.get_state() is None:
        await reply_fixed(message, runtime, lambda: Reply(tr().nothing_to_cancel))
        return
    await end_flow(message, state)
    await reply_fixed(message, runtime, lambda: Reply(tr().cancelled))


async def on_language(message: Message, runtime: BotRuntime) -> None:
    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        return commands.language_menu()

    await run(message, runtime, body, member=False)


async def on_events(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.events(runtime, s, a, n))


async def on_event(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await run(message, runtime, lambda s, a, n: commands.events(runtime, s, a, n))
        return

    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        event_id = await commands.resolve_event_id(session, argument)
        return await commands.event_card(runtime, session, actor, now, event_id)

    await run(message, runtime, body)


async def on_enroll(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await run(message, runtime, lambda s, a, n: commands.events(runtime, s, a, n))
        return

    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        event_id = await commands.resolve_event_id(session, argument)
        return await commands.enroll(session, actor, now, event_id)

    await run(message, runtime, body)


async def on_team(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.teams(runtime, s, a, n))


async def on_invites(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.invites(runtime, s, a, n))


def collect(start: FlowStart, data: dict[str, str]) -> Body:
    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        reply, values = await start(session)
        data.update(values)
        return reply

    return body


async def on_ask(
    message: Message, command: CommandObject, runtime: BotRuntime, state: FSMContext
) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await end_flow(message, state)
        await run(message, runtime, lambda s, a, n: commands.ask_areas(s))
        return
    area_key = parts[0].lower()
    data: dict[str, str] = {}
    if len(parts) == 1:
        body = collect(lambda session: commands.ask_topic(session, area_key), data)
        flow = Flow.ask_topic
    else:
        topic = parts[1].strip()
        body = collect(lambda session: commands.start_ask(session, area_key, topic), data)
        flow = Flow.ask_goal
    sent = await run(message, runtime, body)
    if sent is not None and sent[0].ok:
        await end_flow(message, state)
        await start_flow(state, flow, data, sent[1])


async def on_requests(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.requests(runtime, s, a, n))


async def on_mentorships(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.mentorships(runtime, s, a, n))


async def on_confirm(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.confirmations(runtime, s, a, n))


async def on_profile(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.profile(runtime, s, a, n))


async def on_dnd(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip() or None
    await run(message, runtime, lambda s, a, n: commands.dnd(runtime, s, a, n, argument))


async def finish(message: Message, state: FSMContext, runtime: BotRuntime, body: Body) -> None:
    sent = await run(message, runtime, body)
    if sent is not None and not sent[0].retry:
        await end_flow(message, state)


async def on_ask_topic(message: Message, state: FSMContext, runtime: BotRuntime) -> None:
    area_key = (await state.get_data())["area_key"]
    topic = (message.text or "").strip()
    data: dict[str, str] = {}
    sent = await run(
        message,
        runtime,
        collect(lambda session: commands.start_ask(session, area_key, topic), data),
    )
    if sent is None:
        return
    reply, prompt_message = sent
    if reply.ok:
        await retire_prompt(message, state)
        await start_flow(state, Flow.ask_goal, data, prompt_message)
    elif not reply.retry:
        await end_flow(message, state)


async def on_ask_goal(message: Message, state: FSMContext, runtime: BotRuntime) -> None:
    data = await state.get_data()
    goal = message.text or ""
    await finish(
        message,
        state,
        runtime,
        lambda s, a, n: commands.create_help_request(
            runtime, s, a, n, area_key=data["area_key"], topic=data["topic"], goal=goal
        ),
    )


async def on_help_message(message: Message, state: FSMContext, runtime: BotRuntime) -> None:
    request_id = UUID((await state.get_data())["request_id"])
    text = message.text or ""
    await finish(
        message,
        state,
        runtime,
        lambda s, a, n: commands.offer_help(s, a, n, request_id, text),
    )


async def on_checkin_note(message: Message, state: FSMContext, runtime: BotRuntime) -> None:
    mentorship_id = UUID((await state.get_data())["mentorship_id"])
    note = message.text or ""
    await finish(
        message,
        state,
        runtime,
        lambda s, a, n: commands.checkin(runtime, s, a, n, mentorship_id, note),
    )


async def on_other(message: Message, runtime: BotRuntime) -> None:
    user = message.from_user
    if user is None:
        return
    await send(
        message,
        await anonymous(
            runtime, user, lambda session, now: commands.unknown(runtime, session, user.id)
        ),
    )


async def callback_reply(
    query: CallbackQuery, runtime: BotRuntime, body: Body, *, member: bool = True
) -> Reply | None:
    user = query.from_user
    reply = await respond(runtime, user.id, body, member=member, language_code=user.language_code)
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
        return None
    return reply


async def on_action(
    query: CallbackQuery, callback_data: ActionCallback, runtime: BotRuntime
) -> None:
    reply = await callback_reply(
        query,
        runtime,
        lambda s, a, n: commands.action(
            runtime, s, a, n, callback_data.key, callback_data.entity_id
        ),
    )
    if reply is None:
        return
    await query.answer(plain(reply.text))
    if isinstance(query.message, Message):
        await edit(query.message, f"{query.message.html_text}\n\n{reply.text}", None)


async def on_event_op(
    query: CallbackQuery, callback_data: EventCallback, runtime: BotRuntime
) -> None:
    reply = await callback_reply(
        query,
        runtime,
        lambda s, a, n: commands.event_op(
            runtime, s, a, n, callback_data.op, callback_data.event_id
        ),
    )
    if reply is None:
        return
    navigation = callback_data.op in (EventOp.SHOW, EventOp.LIST)
    await query.answer(None if navigation else plain(reply.text.split("\n", 1)[0]))
    if isinstance(query.message, Message):
        await edit(query.message, reply.text, reply.markup)


async def on_menu(query: CallbackQuery, callback_data: MenuCallback, runtime: BotRuntime) -> None:
    reply = await callback_reply(
        query, runtime, lambda s, a, n: commands.menu(runtime, s, a, n, callback_data.item)
    )
    if reply is None:
        return
    await query.answer()
    if isinstance(query.message, Message):
        await send(query.message, reply)


async def prompt_from_button(
    query: CallbackQuery,
    runtime: BotRuntime,
    state: FSMContext,
    body: Body,
    flow: State,
    data: dict[str, str],
) -> None:
    reply = await callback_reply(query, runtime, body)
    if reply is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await end_flow(query.message, state)
    await start_flow(state, flow, data, await send(query.message, reply))


async def on_help_button(
    query: CallbackQuery, callback_data: HelpCallback, runtime: BotRuntime, state: FSMContext
) -> None:
    request_id = callback_data.request_id
    await prompt_from_button(
        query,
        runtime,
        state,
        lambda s, a, n: commands.help_prompt(s, a, n, request_id),
        Flow.help_message,
        {"request_id": str(request_id)},
    )


async def on_checkin_button(
    query: CallbackQuery, callback_data: CheckinCallback, runtime: BotRuntime, state: FSMContext
) -> None:
    mentorship_id = callback_data.mentorship_id
    await prompt_from_button(
        query,
        runtime,
        state,
        lambda s, a, n: commands.checkin_prompt(s, a, mentorship_id),
        Flow.checkin_note,
        {"mentorship_id": str(mentorship_id)},
    )


async def on_ask_area(
    query: CallbackQuery, callback_data: AskAreaCallback, runtime: BotRuntime, state: FSMContext
) -> None:
    data: dict[str, str] = {}
    reply = await callback_reply(
        query,
        runtime,
        collect(lambda session: commands.ask_topic(session, callback_data.area_key), data),
    )
    if reply is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await end_flow(query.message, state)
    await edit(query.message, reply.text, reply.markup)
    await start_flow(state, Flow.ask_topic, data, query.message)


async def on_close_button(
    query: CallbackQuery, callback_data: CloseCallback, runtime: BotRuntime
) -> None:
    mentorship_id = callback_data.mentorship_id
    reply = await callback_reply(
        query, runtime, lambda s, a, n: commands.close_prompt(s, a, mentorship_id)
    )
    if reply is None:
        return
    await query.answer()
    if isinstance(query.message, Message):
        await send(query.message, reply)


async def on_cancel_button(query: CallbackQuery, state: FSMContext, runtime: BotRuntime) -> None:
    reply = await anonymous(runtime, query.from_user, fixed(lambda: Reply(tr().cancelled)))
    await query.answer(plain(reply.text))
    message = query.message
    if not isinstance(message, Message):
        return
    if (await state.get_data()).get(PROMPT_KEY) == str(message.message_id):
        await state.clear()
    await edit(message, f"{message.html_text}\n\n<i>{reply.text}</i>", None)


async def on_language_button(
    query: CallbackQuery, callback_data: LanguageCallback, runtime: BotRuntime
) -> None:
    locale = callback_data.locale
    reply = await callback_reply(
        query,
        runtime,
        lambda s, a, n: commands.set_language(runtime, s, a, n, locale),
        member=False,
    )
    if reply is None:
        return
    await query.answer(plain(reply.text))
    if isinstance(query.message, Message):
        await edit(query.message, reply.text, reply.markup)
        await apply_commands(query.bot, query.message.chat.id, reply.locale)


async def on_outcome(
    query: CallbackQuery, callback_data: OutcomeCallback, runtime: BotRuntime
) -> None:
    reply = await callback_reply(
        query,
        runtime,
        lambda s, a, n: commands.close(
            runtime, s, a, n, callback_data.mentorship_id, callback_data.outcome
        ),
    )
    if reply is None:
        return
    await query.answer(plain(reply.text))
    if isinstance(query.message, Message):
        await edit(query.message, reply.text, None)


def mark_confirmed(markup: InlineKeyboardMarkup, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button.model_copy(
                    update={"text": button.text.replace(PENDING_MARK, CONFIRMED_MARK, 1)}
                )
                if button.callback_data == data
                else button
                for button in row
            ]
            for row in markup.inline_keyboard
        ]
    )


async def on_confirm_button(
    query: CallbackQuery, callback_data: ConfirmCallback, runtime: BotRuntime
) -> None:
    reply = await callback_reply(
        query,
        runtime,
        lambda s, a, n: commands.confirm(
            runtime, s, a, n, callback_data.fact_type, callback_data.fact_id
        ),
    )
    if reply is None:
        return
    await query.answer(plain(reply.text))
    message = query.message
    if isinstance(message, Message) and message.reply_markup is not None:
        updated = mark_confirmed(message.reply_markup, callback_data.pack())
        if updated != message.reply_markup:
            await message.edit_reply_markup(reply_markup=updated)


async def on_blocked(event: ChatMemberUpdated, runtime: BotRuntime) -> None:
    user_id = event.from_user.id
    await respond_anonymous(runtime, lambda session, now: commands.unlink(session, user_id))
    await apply_commands(event.bot, event.chat.id, None)


def build_router() -> Router:
    router = Router(name="hmm")
    private = F.chat.type == ChatType.PRIVATE
    router.message.filter(private)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.my_chat_member.filter(private)
    router.message.register(on_start_link, CommandStart(deep_link=True))
    router.message.register(on_start, CommandStart())
    for names, handler in (
        (("unlink",), on_unlink),
        (("help",), on_help),
        (("settings",), on_settings),
        (("cancel",), on_cancel),
        (("events",), on_events),
        (("event",), on_event),
        (("enroll",), on_enroll),
        (("team",), on_team),
        (("invites",), on_invites),
        (("ask",), on_ask),
        (("requests",), on_requests),
        (("mentorships",), on_mentorships),
        (("confirm",), on_confirm),
        (("profile", "me"), on_profile),
        (("dnd",), on_dnd),
        (("language",), on_language),
    ):
        router.message.register(handler, Command(*names))
    text = F.text & ~F.text.startswith("/")
    router.message.register(on_ask_topic, Flow.ask_topic, text)
    router.message.register(on_ask_goal, Flow.ask_goal, text)
    router.message.register(on_help_message, Flow.help_message, text)
    router.message.register(on_checkin_note, Flow.checkin_note, text)
    router.message.register(on_other)
    router.callback_query.register(on_action, ActionCallback.filter())
    router.callback_query.register(on_event_op, EventCallback.filter())
    router.callback_query.register(on_menu, MenuCallback.filter())
    router.callback_query.register(on_help_button, HelpCallback.filter())
    router.callback_query.register(on_checkin_button, CheckinCallback.filter())
    router.callback_query.register(on_ask_area, AskAreaCallback.filter())
    router.callback_query.register(on_close_button, CloseCallback.filter())
    router.callback_query.register(on_cancel_button, CancelCallback.filter())
    router.callback_query.register(on_outcome, OutcomeCallback.filter())
    router.callback_query.register(on_confirm_button, ConfirmCallback.filter())
    router.callback_query.register(on_language_button, LanguageCallback.filter())
    router.my_chat_member.register(on_blocked, ChatMemberUpdatedFilter(KICKED))
    return router
