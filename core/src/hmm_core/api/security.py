from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

session_cookie = APIKeyCookie(name="hmm_session", scheme_name="SessionCookie", auto_error=False)
bot_token = HTTPBearer(scheme_name="BotServiceToken", auto_error=False)
parser_token = HTTPBearer(scheme_name="ParserServiceToken", auto_error=False)
telegram_user = APIKeyHeader(
    name="X-Telegram-User-Id", scheme_name="TelegramUser", auto_error=False
)

SECURITY_SCHEMES = {
    scheme.scheme_name: scheme.model.model_dump(mode="json", by_alias=True, exclude_none=True)
    for scheme in (session_cookie, bot_token, parser_token, telegram_user)
}


async def user(
    cookie: Annotated[str | None, Security(session_cookie)],
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
    telegram_user_id: Annotated[str | None, Security(telegram_user)],
) -> None:
    return None


async def member(actor: Annotated[None, Depends(user)]) -> None:
    return None


async def admin(cookie: Annotated[str | None, Security(session_cookie)]) -> None:
    return None


async def bot_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(bot_token)],
) -> None:
    return None


async def parser_service(
    token: Annotated[HTTPAuthorizationCredentials | None, Security(parser_token)],
) -> None:
    return None


UserAuth = Annotated[None, Depends(user)]
MemberAuth = Annotated[None, Depends(member)]
AdminAuth = Annotated[None, Depends(admin)]
BotAuth = Annotated[None, Depends(bot_service)]
ParserAuth = Annotated[None, Depends(parser_service)]
