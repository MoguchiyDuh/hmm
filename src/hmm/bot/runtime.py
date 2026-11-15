from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape

import procrastinate
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hmm import models
from hmm.bot.fmt import View, viewing
from hmm.common import ErrorCode, Problem
from hmm.identity.service import telegram_actor
from hmm.platform.clock import Clock
from hmm.platform.config import Config
from hmm.platform.db import transaction
from hmm.platform.errors import ProblemError

LINK_PATH = "/settings#telegram"


@dataclass(frozen=True, slots=True, kw_only=True)
class BotRuntime:
    config: Config
    sessionmaker: async_sessionmaker[AsyncSession]
    jobs: procrastinate.App
    clock: Clock

    def url(self, path: str) -> str:
        return f"{self.config.public_url}{path}"


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    markup: InlineKeyboardMarkup | None = None
    ok: bool = True
    retry: bool = False


ERROR_TEXT: dict[ErrorCode, str] = {
    ErrorCode.UNAUTHENTICATED: (
        "This Telegram account isn't linked to hmm yet. Get a link code at {url}" + LINK_PATH
    ),
    ErrorCode.BANNED: "Your hmm account is banned.",
    ErrorCode.ONBOARDING_REQUIRED: "Finish onboarding on the web first: {url}/onboarding",
    ErrorCode.CODE_INVALID: (
        "This link code doesn't exist. Generate a new one at {url}" + LINK_PATH
    ),
    ErrorCode.CODE_EXPIRED: (
        "This link code has expired or was already used. Generate a new one at {url}" + LINK_PATH
    ),
    ErrorCode.TELEGRAM_ALREADY_LINKED: (
        "This Telegram account is linked to another hmm user. Unlink it there first."
    ),
    ErrorCode.NOT_FOUND: "Not found, or you can't see it.",
    ErrorCode.FORBIDDEN: "You can't do that.",
    ErrorCode.RESTRICTED: "Your account is restricted from doing that right now.",
    ErrorCode.USER_DND: "That member is in do-not-disturb mode.",
    ErrorCode.INTERNAL_ERROR: "Something went wrong. Try again later.",
}


def problem_text(error: ProblemError, public_url: str) -> str:
    known = ERROR_TEXT.get(error.code)
    if known is not None:
        return known.format(url=public_url)
    title = Problem.build(public_url=public_url, code=error.code).title
    return f"{title}: {error.detail}" if error.detail else f"{title}."


def validation_text(error: ValidationError) -> str:
    issue = error.errors()[0]
    field = ".".join(str(part) for part in issue["loc"]).replace("_", " ")
    return f"{escape(field.capitalize(), quote=False)}: {escape(issue['msg'], quote=False)}."


def failure(error: ProblemError | ValidationError, public_url: str) -> Reply:
    if isinstance(error, ValidationError):
        return Reply(validation_text(error), ok=False, retry=True)
    return Reply(escape(problem_text(error, public_url), quote=False), ok=False)


type Body = Callable[[AsyncSession, models.User, datetime], Awaitable[Reply]]


async def respond(
    runtime: BotRuntime, telegram_user_id: int, body: Body, *, member: bool = True
) -> Reply:
    now = runtime.clock.now()
    try:
        async with transaction(runtime.sessionmaker) as session:
            actor = await telegram_actor(session, telegram_user_id, now, member=member)
            view = View(public_url=runtime.config.public_url, now=now, timezone=actor.timezone)
            with viewing(view):
                return await body(session, actor, now)
    except (ProblemError, ValidationError) as error:
        return failure(error, runtime.config.public_url)


type AnonymousBody = Callable[[AsyncSession, datetime], Awaitable[Reply]]


async def respond_anonymous(runtime: BotRuntime, body: AnonymousBody) -> Reply:
    now = runtime.clock.now()
    try:
        async with transaction(runtime.sessionmaker) as session:
            with viewing(View(public_url=runtime.config.public_url, now=now)):
                return await body(session, now)
    except (ProblemError, ValidationError) as error:
        return failure(error, runtime.config.public_url)
