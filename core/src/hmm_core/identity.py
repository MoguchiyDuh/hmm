import hashlib
import hmac
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from fastapi import Response
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    Contract,
    ErrorCode,
    OnboardingStatus,
)
from pydantic import AwareDatetime, JsonValue, ValidationError
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.api.errors import ProblemError
from hmm_core.audit import record_audit
from hmm_core.db.models import GithubSnapshot, User, UserSession
from hmm_core.db.settings import load_settings
from hmm_core.github import GithubUser
from hmm_core.jobs import JobName, defer_job

SESSION_COOKIE = "hmm_session"
SESSION_REFRESH = timedelta(minutes=5)
GITHUB_STALE = timedelta(hours=24)
ONBOARDED = frozenset({OnboardingStatus.COMPLETED, OnboardingStatus.REOPENED})


class OAuthState(Contract):
    state: str
    verifier: str
    next: str | None
    expires_at: AwareDatetime


def _mac(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def sign_state(value: OAuthState, secret: str) -> str:
    body = urlsafe_b64encode(value.model_dump_json().encode()).decode().rstrip("=")
    return f"{body}.{_mac(body, secret)}"


def unsign_state(value: str | None, secret: str) -> OAuthState | None:
    if not value:
        return None
    body, _, mac = value.rpartition(".")
    if not body or not hmac.compare_digest(mac.encode(), _mac(body, secret).encode()):
        return None
    try:
        return OAuthState.model_validate_json(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, ValidationError):
        return None


def pkce_challenge(verifier: str) -> str:
    return urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def safe_next(value: str | None) -> str | None:
    if (
        value
        and value.startswith("/")
        and not value.startswith(("//", "/\\"))
        and value.isprintable()
    ):
        return value
    return None


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def session_ttl(session: AsyncSession) -> timedelta:
    return timedelta(days=(await load_settings(session)).session_ttl_days)


def set_session_cookie(response: Response, token: str, ttl: timedelta) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(ttl.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


async def create_session(
    session: AsyncSession,
    user: User,
    now: datetime,
    ttl: timedelta,
    user_agent: str | None,
    ip: str | None,
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=now + ttl,
            last_used_at=now,
            user_agent=user_agent,
            ip=ip,
        )
    )
    user.last_seen_at = now
    await session.flush()
    return token


async def resolve_session(
    session: AsyncSession, token: str, now: datetime
) -> tuple[User, timedelta | None]:
    row = (
        await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).one_or_none()
    if row is None or row[0].expires_at <= now:
        raise ProblemError(ErrorCode.UNAUTHENTICATED)
    record, user = row
    if user.banned_at is not None:
        raise ProblemError(ErrorCode.BANNED)
    if now - record.last_used_at < SESSION_REFRESH:
        return user, None
    ttl = await session_ttl(session)
    record.last_used_at = now
    record.expires_at = now + ttl
    user.last_seen_at = now
    await session.flush()
    return user, ttl


async def delete_sessions(session: AsyncSession, user_id: UUID, token: str | None = None) -> None:
    statement = delete(UserSession).where(UserSession.user_id == user_id)
    if token is not None:
        statement = statement.where(UserSession.token_hash == hash_token(token))
    await session.execute(statement)


async def _grant_bootstrap_admin(session: AsyncSession, user: User) -> None:
    user.is_admin = True
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.ADMIN_GRANT,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        before={"is_admin": False},
        after={"is_admin": True},
        reason="ADMIN_GITHUB_LOGINS bootstrap",
    )


async def sign_in(
    session: AsyncSession,
    jobs: procrastinate.App,
    profile: GithubUser,
    admin_logins: frozenset[str],
    now: datetime,
) -> User:
    listed = profile.login.lower() in admin_logins
    user = await session.scalar(select(User).where(User.github_id == profile.id).with_for_update())
    if user is None:
        user = User(
            github_id=profile.id,
            github_login=profile.login,
            display_name=((profile.name or "").strip() or profile.login)[:100],
            avatar_url=profile.avatar_url,
            onboarding_status=OnboardingStatus.NOT_STARTED,
        )
        session.add(user)
        await session.flush()
        if listed:
            await _grant_bootstrap_admin(session, user)
        await defer_job(session, jobs, JobName.GITHUB_SYNC, user_id=str(user.id))
        return user
    user.github_login = profile.login
    if user.banned_at is not None:
        await session.flush()
        return user
    if (
        listed
        and not user.is_admin
        and not await session.scalar(select(exists().where(User.is_admin)))
    ):
        await _grant_bootstrap_admin(session, user)
    fetched_at = await session.scalar(
        select(GithubSnapshot.fetched_at).where(GithubSnapshot.user_id == user.id)
    )
    if fetched_at is None or now - fetched_at > GITHUB_STALE:
        await defer_job(session, jobs, JobName.GITHUB_SYNC, user_id=str(user.id))
    await session.flush()
    return user


async def lock_user(session: AsyncSession, user_id: UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return user


async def admin_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).where(User.is_admin)) or 0


async def edit_user(
    session: AsyncSession,
    user: User,
    changes: dict[str, str],
    actor: UUID,
    reason: str | None,
) -> None:
    changed: dict[str, JsonValue] = {
        key: value for key, value in changes.items() if getattr(user, key) != value
    }
    if not changed:
        return
    before = {key: getattr(user, key) for key in changed}
    for key, value in changed.items():
        setattr(user, key, value)
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.USER_EDIT,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        before=before,
        after=changed,
        reason=reason,
    )


async def set_admin(
    session: AsyncSession, user: User, value: bool, actor: UUID, reason: str | None
) -> None:
    if user.is_admin == value:
        return
    if value and user.banned_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Unban the user before granting admin")
    if not value:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('admins'))"))
        if await admin_count(session) <= 1:
            raise ProblemError(ErrorCode.LAST_ADMIN)
    user.is_admin = value
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.ADMIN_GRANT if value else AuditAction.ADMIN_REVOKE,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        before={"is_admin": not value},
        after={"is_admin": value},
        reason=reason,
    )


async def ban_user(
    session: AsyncSession, user: User, reason: str, actor: UUID, now: datetime
) -> None:
    if user.banned_at is not None:
        return
    if user.is_admin:
        raise ProblemError(ErrorCode.INVALID_STATE, "Revoke admin before banning")
    user.banned_at = now
    user.ban_reason = reason
    await delete_sessions(session, user.id)
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.USER_BAN,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        before={"banned_at": None, "ban_reason": None},
        after={"banned_at": now.isoformat(), "ban_reason": reason},
        reason=reason,
    )


async def unban_user(session: AsyncSession, user: User, actor: UUID, reason: str | None) -> None:
    if user.banned_at is None:
        return
    before: dict[str, JsonValue] = {
        "banned_at": user.banned_at.isoformat(),
        "ban_reason": user.ban_reason,
    }
    user.banned_at = None
    user.ban_reason = None
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.USER_UNBAN,
        entity_type=AuditEntityType.USER,
        entity_id=user.id,
        before=before,
        after={"banned_at": None, "ban_reason": None},
        reason=reason,
    )
