import logging
import secrets
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from hmm_contracts.common import ErrorCode, RestrictionKind
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.schemas.identity import (
    AccountAction,
    CurrentUser,
    TelegramLink,
    TelegramLinkCode,
)
from hmm_core.api.security import UserAuth
from hmm_core.clock import CurrentClock
from hmm_core.config import Config
from hmm_core.db import Session
from hmm_core.db.models import MemberRestriction, Notification, User
from hmm_core.db.models import TelegramLink as TelegramLinkRow
from hmm_core.github import GithubError, authorize_url, exchange_code, fetch_user
from hmm_core.identity import (
    ONBOARDED,
    SESSION_COOKIE,
    OAuthState,
    clear_session_cookie,
    create_session,
    delete_sessions,
    pkce_challenge,
    safe_next,
    session_ttl,
    set_session_cookie,
    sign_in,
    sign_state,
    unsign_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["identity"], responses=PROBLEM_RESPONSES)

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


async def current_user(session: AsyncSession, actor: User, now: datetime) -> CurrentUser:
    link = await session.get(TelegramLinkRow, actor.id)
    unread = await session.scalar(
        select(func.count()).where(Notification.user_id == actor.id, Notification.read_at.is_(None))
    )
    restricted = set(
        await session.scalars(
            select(MemberRestriction.kind).where(
                MemberRestriction.user_id == actor.id,
                MemberRestriction.lifted_at.is_(None),
                (MemberRestriction.until.is_(None)) | (MemberRestriction.until > now),
            )
        )
    )
    allowed = {AccountAction.LINK_TELEGRAM}
    if link is not None:
        allowed.add(AccountAction.UNLINK_TELEGRAM)
    if actor.onboarding_status in ONBOARDED:
        allowed |= {AccountAction.REFRESH_GITHUB, AccountAction.REQUEST_HELP}
        if RestrictionKind.CREATING_PROJECTS not in restricted:
            allowed.add(AccountAction.CREATE_PROJECT)
        if RestrictionKind.CREATING_EVENTS not in restricted:
            allowed.add(AccountAction.CREATE_EVENT)
    return CurrentUser(
        id=actor.id,
        display_name=actor.display_name,
        avatar_url=actor.avatar_url,
        github_login=actor.github_login,
        github_url=f"https://github.com/{actor.github_login}",
        is_admin=actor.is_admin,
        onboarding_status=actor.onboarding_status,
        dnd_until=actor.dnd_until,
        locale=actor.locale,
        telegram=TelegramLink(
            linked=link is not None,
            username=link.username if link else None,
            linked_at=link.linked_at if link else None,
            broken_at=link.broken_at if link else None,
        ),
        unread_count=unread or 0,
        allowed_actions=[action for action in AccountAction if action in allowed],
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
    state = OAuthState(
        state=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(64),
        next=safe_next(next),
        expires_at=clock.now() + OAUTH_TTL,
    )
    response = RedirectResponse(
        authorize_url(client_id, callback_url(config), state.state, pkce_challenge(state.verifier)),
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        OAUTH_COOKIE,
        sign_state(state, secret),
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
    request: Request,
    session: Session,
    clock: CurrentClock,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> Response:
    config, client_id, client_secret, secret = oauth_config(request)
    now = clock.now()
    stored = unsign_state(request.cookies.get(OAUTH_COOKIE), secret)
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
    actor = await sign_in(session, request.app.state.jobs, profile, config.admin_github_logins, now)
    if actor.banned_at is not None:
        return oauth_redirect(f"/login?error={ErrorCode.BANNED}")
    ttl = await session_ttl(session)
    token = await create_session(
        session,
        actor,
        now,
        ttl,
        request.headers.get("user-agent"),
        client_ip(request),
    )
    response = oauth_redirect(stored.next or "/onboarding")
    set_session_cookie(response, token, ttl)
    return response


@router.post(
    "/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Delete current session"
)
async def logout(actor: UserAuth, request: Request, response: Response, session: Session) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token is not None:
        await delete_sessions(session, actor.id, token)
    clear_session_cookie(response)


@router.post(
    "/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="Delete all own sessions"
)
async def logout_all(actor: UserAuth, response: Response, session: Session) -> None:
    await delete_sessions(session, actor.id)
    clear_session_cookie(response)


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
) -> TelegramLinkCode:
    not_implemented()


@router.delete("/me/telegram", status_code=status.HTTP_204_NO_CONTENT, summary="Unlink Telegram")
async def unlink_telegram(actor: UserAuth) -> None:
    not_implemented()
