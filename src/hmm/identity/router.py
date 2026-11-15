import logging
import secrets
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode
from hmm.identity import service as identity
from hmm.identity.github import GithubError, authorize_url, exchange_code, fetch_user
from hmm.identity.schemas import (
    CurrentUser,
    TelegramLink,
    TelegramLinkCode,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.config import Config
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.jobs import Jobs
from hmm.platform.security import UserAuth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)

OAUTH_COOKIE = "hmm_oauth"
OAUTH_COOKIE_PATH = "/api/v1/auth/github"
OAUTH_TTL = timedelta(minutes=10)


def oauth_config(request: Request) -> tuple[Config, str, str, str]:
    config: Config = request.app.state.config
    if not (config.github_client_id and config.github_client_secret and config.session_secret):
        raise RuntimeError("GitHub OAuth is not configured")
    return config, config.github_client_id, config.github_client_secret, config.session_secret


def callback_url(config: Config) -> str:
    return f"{config.public_url}/api/v1/auth/github/callback"


def client_ip(request: Request) -> str | None:
    try:
        return str(ip_address(request.client.host)) if request.client else None
    except ValueError:
        return None


def oauth_redirect(location: str) -> RedirectResponse:
    response = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(
        OAUTH_COOKIE, path=OAUTH_COOKIE_PATH, secure=True, httponly=True, samesite="lax"
    )
    return response


async def current_user(session: AsyncSession, actor: models.User, now: datetime) -> CurrentUser:
    state = await identity.account_state(session, actor, now)
    link = state.telegram
    return CurrentUser(
        id=actor.id,
        display_name=actor.display_name,
        avatar_url=actor.avatar_url,
        github_login=actor.github_login,
        github_url=f"https://github.com/{actor.github_login}",
        is_admin=actor.is_admin,
        onboarding_status=actor.onboarding_status,
        dnd_until=identity.dnd_until(actor),
        locale=actor.locale,
        telegram=TelegramLink(
            linked=link is not None,
            username=link.username if link else None,
            linked_at=link.linked_at if link else None,
            broken_at=link.broken_at if link else None,
        ),
        unread_count=state.unread_count,
        allowed_actions=state.allowed_actions,
    )


@router.get(
    "/auth/github/login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Sign in with GitHub",
)
async def login(
    request: Request, clock: CurrentClock, next: Annotated[str | None, Query()] = None
) -> Response:
    config, client_id, _, secret = oauth_config(request)
    state = identity.OAuthState(
        state=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(64),
        next=identity.safe_next(next),
        expires_at=clock.now() + OAUTH_TTL,
    )
    response = RedirectResponse(
        authorize_url(
            client_id, callback_url(config), state.state, identity.pkce_challenge(state.verifier)
        ),
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        OAUTH_COOKIE,
        identity.sign_state(state, secret),
        max_age=int(OAUTH_TTL.total_seconds()),
        path=OAUTH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get(
    "/auth/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Complete GitHub sign-in",
)
async def github_callback(
    jobs: Jobs,
    request: Request,
    session: Session,
    clock: CurrentClock,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> Response:
    config, client_id, client_secret, secret = oauth_config(request)
    now = clock.now()
    stored = identity.unsign_state(request.cookies.get(OAUTH_COOKIE), secret)
    if (
        code is None
        or state is None
        or stored is None
        or stored.expires_at <= now
        or not secrets.compare_digest(state.encode(), stored.state.encode())
    ):
        return oauth_redirect(f"/login?error={ErrorCode.BAD_REQUEST}")
    try:
        token = await exchange_code(
            request.app.state.http,
            client_id,
            client_secret,
            callback_url(config),
            code,
            stored.verifier,
        )
        profile = await fetch_user(request.app.state.http, token)
    except GithubError:
        logger.exception("GitHub sign-in failed")
        return oauth_redirect(f"/login?error={ErrorCode.INTERNAL_ERROR}")
    actor = await identity.sign_in(session, jobs, profile, config.admin_github_logins, now)
    if actor.banned_at is not None:
        return oauth_redirect(f"/login?error={ErrorCode.BANNED}")
    ttl = await identity.session_ttl(session)
    token = await identity.create_session(
        session,
        actor,
        now,
        ttl,
        request.headers.get("user-agent"),
        client_ip(request),
    )
    response = oauth_redirect(stored.next or "/onboarding")
    identity.set_session_cookie(response, token, ttl)
    return response


@router.post(
    "/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Delete current session"
)
async def logout(actor: UserAuth, request: Request, response: Response, session: Session) -> None:
    token = request.cookies.get(identity.SESSION_COOKIE)
    if token is not None:
        await identity.delete_sessions(session, actor.id, token)
    identity.clear_session_cookie(response)


@router.post(
    "/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="Delete all own sessions"
)
async def logout_all(actor: UserAuth, response: Response, session: Session) -> None:
    await identity.delete_sessions(session, actor.id)
    identity.clear_session_cookie(response)


@router.get("/me", status_code=status.HTTP_200_OK, summary="Get current user")
async def get_current_user(actor: UserAuth, session: Session, clock: CurrentClock) -> CurrentUser:
    return await current_user(session, actor, clock.now())


@router.post(
    "/me/telegram/link-code",
    status_code=status.HTTP_201_CREATED,
    summary="Create Telegram link code",
)
async def create_telegram_link_code(
    actor: UserAuth,
    request: Request,
    session: Session,
    clock: CurrentClock,
) -> TelegramLinkCode:
    config: Config = request.app.state.config
    if not config.telegram_bot_username:
        raise RuntimeError("Telegram bot username is not configured")
    record = await identity.create_telegram_code(session, actor.id, clock.now())
    return TelegramLinkCode(
        code=record.code,
        deep_link=f"https://t.me/{config.telegram_bot_username}?start={record.code}",
        expires_at=record.expires_at,
    )


@router.delete("/me/telegram", status_code=status.HTTP_204_NO_CONTENT, summary="Unlink Telegram")
async def unlink_telegram(actor: UserAuth, session: Session) -> None:
    await identity.unlink_user_telegram(session, actor.id)
