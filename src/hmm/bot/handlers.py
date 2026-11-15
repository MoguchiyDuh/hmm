from datetime import datetime
from uuid import UUID

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import KICKED, ChatMemberUpdatedFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.bot import commands
from hmm.bot.callbacks import (
    ActionCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    EventCallback,
    HelpCallback,
    OutcomeCallback,
)
from hmm.bot.fmt import clip
from hmm.bot.runtime import Body, BotRuntime, Reply, respond, respond_anonymous

TOAST_LIMIT = 200
CONFIRMED_MARK = "☑"
PENDING_MARK = "☐"


class Flow(StatesGroup):
    ask_goal = State()
    help_message = State()
    checkin_note = State()


async def send(message: Message, reply: Reply) -> None:
    await message.answer(reply.text, reply_markup=reply.markup)


async def run(message: Message, runtime: BotRuntime, body: Body) -> Reply | None:
    if message.from_user is None:
        return None
    reply = await respond(runtime, message.from_user.id, body)
    await send(message, reply)
    return reply


def plain(text: str) -> str:
    return clip(text.replace("<b>", "").replace("</b>", ""), TOAST_LIMIT)


async def on_start_link(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    user = message.from_user
    if user is None or command.args is None:
        return
    code = command.args.strip()
    reply = await respond_anonymous(
        runtime,
        lambda session, now: commands.link_account(
            session,
            now,
            code=code,
            telegram_user_id=user.id,
            chat_id=message.chat.id,
            username=user.username,
        ),
    )
    await send(message, reply)


async def on_start(message: Message, runtime: BotRuntime) -> None:
    user = message.from_user
    if user is None:
        return
    await send(
        message,
        await respond_anonymous(
            runtime, lambda session, now: commands.greet(runtime, session, user.id)
        ),
    )


async def on_unlink(message: Message, runtime: BotRuntime, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    await state.clear()
    await send(
        message,
        await respond_anonymous(runtime, lambda session, now: commands.unlink(session, user.id)),
    )


async def on_help(message: Message) -> None:
    await message.answer(commands.HELP_TEXT)


async def on_settings(message: Message, runtime: BotRuntime) -> None:
    await send(message, commands.settings_reply(runtime))


async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


async def on_events(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.events(runtime, s, a, n))


async def on_event(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await send(message, commands.usage("event"))
        return

    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        event_id = await commands.resolve_event_id(session, argument)
        return await commands.event_card(runtime, session, actor, now, event_id)

    await run(message, runtime, body)


async def on_enroll(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await send(message, commands.usage("enroll"))
        return

    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        event_id = await commands.resolve_event_id(session, argument)
        return await commands.enroll(session, actor, now, event_id)

    await run(message, runtime, body)


async def on_team(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.teams(runtime, s, a, n))


async def on_invites(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.invites(runtime, s, a, n))


async def on_ask(
    message: Message, command: CommandObject, runtime: BotRuntime, state: FSMContext
) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await run(message, runtime, lambda s, a, n: commands.ask_areas(s))
        return
    area_key, topic = parts[0].lower(), parts[1].strip()
    data: dict[str, str] = {}

    async def body(session: AsyncSession, actor: models.User, now: datetime) -> Reply:
        reply, values = await commands.start_ask(session, area_key, topic)
        data.update(values)
        return reply

    reply = await run(message, runtime, body)
    if reply is not None and reply.ok:
        await state.set_state(Flow.ask_goal)
        await state.set_data(data)


async def on_requests(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.requests(runtime, s, a, n))


async def on_mentorships(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.mentorships(runtime, s, a, n))


async def on_confirm(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.confirmations(runtime, s, a, n))


async def on_me(message: Message, runtime: BotRuntime) -> None:
    await run(message, runtime, lambda s, a, n: commands.me(runtime, s, a, n))


async def on_dnd(message: Message, command: CommandObject, runtime: BotRuntime) -> None:
    argument = (command.args or "").strip() or None
    await run(message, runtime, lambda s, a, n: commands.dnd(runtime, s, a, n, argument))


async def finish(message: Message, state: FSMContext, runtime: BotRuntime, body: Body) -> None:
    reply = await run(message, runtime, body)
    if reply is not None and not reply.retry:
        await state.clear()


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
    await on_start(message, runtime)


async def callback_reply(query: CallbackQuery, runtime: BotRuntime, body: Body) -> Reply:
    return await respond(runtime, query.from_user.id, body)


async def edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message:
            raise


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
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
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
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        await send(query.message, reply)


async def prompt(
    query: CallbackQuery,
    runtime: BotRuntime,
    state: FSMContext,
    body: Body,
    flow: State | None,
    data: dict[str, str],
) -> None:
    reply = await callback_reply(query, runtime, body)
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
        return
    await query.answer()
    if flow is not None:
        await state.set_state(flow)
        await state.set_data(data)
    if isinstance(query.message, Message):
        await send(query.message, reply)


async def on_help_button(
    query: CallbackQuery, callback_data: HelpCallback, runtime: BotRuntime, state: FSMContext
) -> None:
    request_id = callback_data.request_id
    await prompt(
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
    await prompt(
        query,
        runtime,
        state,
        lambda s, a, n: commands.checkin_prompt(s, a, mentorship_id),
        Flow.checkin_note,
        {"mentorship_id": str(mentorship_id)},
    )


async def on_close_button(
    query: CallbackQuery, callback_data: CloseCallback, runtime: BotRuntime, state: FSMContext
) -> None:
    mentorship_id = callback_data.mentorship_id
    await prompt(
        query,
        runtime,
        state,
        lambda s, a, n: commands.close_prompt(s, a, mentorship_id),
        None,
        {},
    )


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
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
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
    if not reply.ok:
        await query.answer(plain(reply.text), show_alert=True)
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


def build_router() -> Router:
    router = Router(name="hmm")
    private = F.chat.type == ChatType.PRIVATE
    router.message.filter(private)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.my_chat_member.filter(private)
    router.message.register(on_start_link, CommandStart(deep_link=True))
    router.message.register(on_start, CommandStart())
    for name, handler in (
        ("unlink", on_unlink),
        ("help", on_help),
        ("settings", on_settings),
        ("cancel", on_cancel),
        ("events", on_events),
        ("event", on_event),
        ("enroll", on_enroll),
        ("team", on_team),
        ("invites", on_invites),
        ("ask", on_ask),
        ("requests", on_requests),
        ("mentorships", on_mentorships),
        ("confirm", on_confirm),
        ("me", on_me),
        ("dnd", on_dnd),
    ):
        router.message.register(handler, Command(name))
    text = F.text & ~F.text.startswith("/")
    router.message.register(on_ask_goal, Flow.ask_goal, text)
    router.message.register(on_help_message, Flow.help_message, text)
    router.message.register(on_checkin_note, Flow.checkin_note, text)
    router.message.register(on_other)
    router.callback_query.register(on_action, ActionCallback.filter())
    router.callback_query.register(on_event_op, EventCallback.filter())
    router.callback_query.register(on_help_button, HelpCallback.filter())
    router.callback_query.register(on_checkin_button, CheckinCallback.filter())
    router.callback_query.register(on_close_button, CloseCallback.filter())
    router.callback_query.register(on_outcome, OutcomeCallback.filter())
    router.callback_query.register(on_confirm_button, ConfirmCallback.filter())
    router.my_chat_member.register(on_blocked, ChatMemberUpdatedFilter(KICKED))
    return router
