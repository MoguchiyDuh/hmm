from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from hmm_contracts.common import ErrorCode
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import (
    ONBOARDED,
    SESSION_COOKIE,
    require_service_token,
    resolve_session,
    resolve_telegram_user,
    set_session_cookie,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.config import Config
from hmm_core.platform.db import Session
from hmm_core.platform.errors import ProblemError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

session_cookie = APIKeyCookie(name=SESSION_COOKIE, scheme_name="SessionCookie", auto_error=False)
bot_token = HTTPBearer(scheme_name="BotServiceToken", auto_error=False)
parser_token = HTTPBearer(scheme_name="ParserServiceToken", auto_error=False)
telegram_user = APIKeyHeader(
    name="X-Telegram-User-Id", scheme_name="TelegramUser", auto_error=False
)

SECURITY_SCHEMES = {
    scheme.scheme_name: scheme.model.model_dump(mode="json", by_alias=True, exclude_none=True)
    for scheme in (session_cookie, bot_token, parser_token, telegram_user)
}


async def web_user(
    request: Request, response: Response, session: AsyncSession, now: datetime, token: str
) -> models.User:
    config: Config = request.app.state.config
    if request.method not in SAFE_METHODS and request.headers.get("origin") != config.public_url:
        raise ProblemError(ErrorCode.FORBIDDEN, "Origin does not match PUBLIC_URL")
    actor, ttl = await resolve_session(session, token, now)
    if ttl is not None:
        set_session_cookie(response, token, ttl)
    return actor


async def user(
    request: Request,
    response: Response,
    session: Session,
    clock: CurrentClock,
    cookie: Annotated[str | None, Security(session_cookie)],
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
    telegram_user_id: Annotated[str | None, Security(telegram_user)],
) -> models.User:
    if cookie is not None and (
        request.headers.get("authorization") is not None or telegram_user_id is not None
    ):
        raise ProblemError(ErrorCode.BAD_REQUEST, "Send a session cookie or bot credentials")
    if cookie is not None:
        return await web_user(request, response, session, clock.now(), cookie)
    if token is not None:
        config: Config = request.app.state.config
        require_service_token(token.credentials, config.bot_service_token)
        return await resolve_telegram_user(session, telegram_user_id)
    raise ProblemError(ErrorCode.UNAUTHENTICATED)


async def member(actor: Annotated[models.User, Depends(user)]) -> models.User:
    if actor.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.ONBOARDING_REQUIRED)
    return actor


async def admin(
    request: Request,
    response: Response,
    session: Session,
    clock: CurrentClock,
    cookie: Annotated[str | None, Security(session_cookie)],
) -> models.User:
    if cookie is None:
        raise ProblemError(ErrorCode.UNAUTHENTICATED)
    if "authorization" in request.headers or "x-telegram-user-id" in request.headers:
        raise ProblemError(ErrorCode.BAD_REQUEST, "Send a session cookie or bot credentials")
    actor = await web_user(request, response, session, clock.now(), cookie)
    if not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return actor


async def bot_service(
    request: Request,
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
) -> None:
    config: Config = request.app.state.config
    require_service_token(token.credentials if token else None, config.bot_service_token)


async def parser_service(
    request: Request,
    token: Annotated[HTTPAuthorizationCredentials | None, Security(parser_token)],
) -> None:
    config: Config = request.app.state.config
    require_service_token(token.credentials if token else None, config.parser_service_token)


UserAuth = Annotated[models.User, Depends(user)]
MemberAuth = Annotated[models.User, Depends(member)]
AdminAuth = Annotated[models.User, Depends(admin)]
BotAuth = Annotated[None, Depends(bot_service)]
ParserAuth = Annotated[None, Depends(parser_service)]
