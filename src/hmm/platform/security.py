from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Response, Security
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode
from hmm.identity.service import (
    SESSION_COOKIE,
    require_member,
    resolve_session,
    set_session_cookie,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.config import Config
from hmm.platform.db import Session
from hmm.platform.errors import ProblemError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

session_cookie = APIKeyCookie(name=SESSION_COOKIE, scheme_name="SessionCookie", auto_error=False)


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
) -> models.User:
    if cookie is None:
        raise ProblemError(ErrorCode.UNAUTHENTICATED)
    return await web_user(request, response, session, clock.now(), cookie)


async def member(actor: Annotated[models.User, Depends(user)]) -> models.User:
    return require_member(actor)


async def admin(actor: Annotated[models.User, Depends(user)]) -> models.User:
    if not actor.is_admin:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return actor


UserAuth = Annotated[models.User, Depends(user)]
MemberAuth = Annotated[models.User, Depends(member)]
AdminAuth = Annotated[models.User, Depends(admin)]
