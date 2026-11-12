from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from hmm_contracts.common import ErrorCode
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.api.errors import ProblemError, not_implemented
from hmm_core.clock import CurrentClock
from hmm_core.config import Config
from hmm_core.db import Session
from hmm_core.db.models import User
from hmm_core.identity import ONBOARDED, SESSION_COOKIE, resolve_session, set_session_cookie

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
) -> User:
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
) -> User:
    if cookie is not None and token is not None:
        raise ProblemError(ErrorCode.BAD_REQUEST, "Send a session cookie or bot credentials")
    if cookie is not None:
        return await web_user(request, response, session, clock.now(), cookie)
    if token is not None:
        not_implemented()
    raise ProblemError(ErrorCode.UNAUTHENTICATED)


async def member(actor: Annotated[User, Depends(user)]) -> User:
    if actor.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.ONBOARDING_REQUIRED)
    return actor


async def admin(
    request: Request,
    response: Response,
    session: Session,
    clock: CurrentClock,
    cookie: Annotated[str | None, Security(session_cookie)],
) -> User:
    if cookie is None:
        raise ProblemError(ErrorCode.UNAUTHENTICATED)
    actor = await web_user(request, response, session, clock.now(), cookie)
    if not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return actor


async def bot_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
) -> None:
    return None


async def parser_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(parser_token)],
) -> None:
    return None


UserAuth = Annotated[User, Depends(user)]
MemberAuth = Annotated[User, Depends(member)]
AdminAuth = Annotated[User, Depends(admin)]
BotAuth = Annotated[None, Depends(bot_service)]
ParserAuth = Annotated[None, Depends(parser_service)]
