from typing import Annotated

from fastapi import Depends, Header, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

session_cookie = APIKeyCookie(name="hmm_session", scheme_name="SessionCookie", auto_error=False)
bot_token = HTTPBearer(scheme_name="BotServiceToken", auto_error=False)
parser_token = HTTPBearer(scheme_name="ParserServiceToken", auto_error=False)

SECURITY_SCHEMES = {
    scheme.scheme_name: scheme.model.model_dump(mode="json", by_alias=True, exclude_none=True)
    for scheme in (session_cookie, bot_token, parser_token)
}


async def session_user(cookie: Annotated[str | None, Security(session_cookie)]) -> None:
    return None


async def bot_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
) -> None:
    return None


async def bot_acting_user(
    service: Annotated[None, Depends(bot_service)],
    telegram_user_id: Annotated[int, Header(alias="X-Telegram-User-Id")],
) -> None:
    return None


async def parser_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(parser_token)],
) -> None:
    return None


member = session_user
admin = session_user
