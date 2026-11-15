from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape

import procrastinate
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hmm import models
from hmm.bot.fmt import TEXTS, View, viewing
from hmm.bot.i18n import telegram_locale
from hmm.common import Locale, Problem
from hmm.identity.service import telegram_actor
from hmm.platform.clock import Clock
from hmm.platform.config import Config
from hmm.platform.db import transaction
from hmm.platform.errors import ProblemError


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
    locale: Locale | None = None
    toast: str | None = None


def problem_text(error: ProblemError, public_url: str, locale: Locale) -> str:
    known = TEXTS[locale].errors.get(error.code)
    if known is not None:
        return known.format(url=public_url)
    title = Problem.build(public_url=public_url, code=error.code).title
    return f"{title}: {error.detail}" if error.detail else f"{title}."


def validation_text(error: ValidationError, locale: Locale) -> str:
    texts = TEXTS[locale]
    issue = error.errors()[0]
    path = [str(part) for part in issue["loc"]]
    field = texts.field_names.get(path[-1]) if path else None
    if field is None:
        field = " ".join(path).replace("_", " ").capitalize()
    template = texts.validation.get(issue["type"])
    message = template.format(**issue.get("ctx", {})) if template else issue["msg"]
    return texts.invalid_field.format(
        field=escape(field, quote=False), message=escape(message, quote=False)
    )


def failure(error: ProblemError | ValidationError, public_url: str, locale: Locale) -> Reply:
    if isinstance(error, ValidationError):
        return Reply(validation_text(error, locale), ok=False, retry=True)
    return Reply(escape(problem_text(error, public_url, locale), quote=False), ok=False)


async def linked_locale(session: AsyncSession, telegram_user_id: int) -> Locale | None:
    value = await session.scalar(
        select(models.User.locale)
        .join(models.TelegramLink, models.TelegramLink.user_id == models.User.id)
        .where(models.TelegramLink.telegram_user_id == telegram_user_id)
    )
    return Locale(value) if value else None


type Body = Callable[[AsyncSession, models.User, datetime], Awaitable[Reply]]


async def respond(
    runtime: BotRuntime,
    telegram_user_id: int,
    body: Body,
    *,
    member: bool = True,
    language_code: str | None = None,
) -> Reply:
    now = runtime.clock.now()
    locale = telegram_locale(language_code)
    try:
        async with transaction(runtime.sessionmaker) as session:
            actor = await telegram_actor(session, telegram_user_id, now, member=member)
            locale = Locale(actor.locale)
            view = View(
                public_url=runtime.config.public_url,
                now=now,
                timezone=actor.timezone,
                locale=locale,
            )
            with viewing(view):
                return await body(session, actor, now)
    except (ProblemError, ValidationError) as error:
        return failure(error, runtime.config.public_url, locale)


type AnonymousBody = Callable[[AsyncSession, datetime], Awaitable[Reply]]


async def respond_anonymous(
    runtime: BotRuntime,
    body: AnonymousBody,
    *,
    telegram_user_id: int | None = None,
    language_code: str | None = None,
) -> Reply:
    now = runtime.clock.now()
    locale = telegram_locale(language_code)
    try:
        async with transaction(runtime.sessionmaker) as session:
            if telegram_user_id is not None:
                locale = await linked_locale(session, telegram_user_id) or locale
            view = View(public_url=runtime.config.public_url, now=now, locale=locale)
            with viewing(view):
                return await body(session, now)
    except (ProblemError, ValidationError) as error:
        return failure(error, runtime.config.public_url, locale)
