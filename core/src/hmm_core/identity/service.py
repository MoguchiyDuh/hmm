import hashlib
import hmac
import secrets
from base64 import b32encode, urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
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
    RestrictionKind,
)
from pydantic import AwareDatetime, JsonValue, ValidationError
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.confirmations import refresh_member_confirmations
from hmm_core.identity.github import GithubUser
from hmm_core.identity.schemas import AccountAction
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings

SESSION_COOKIE = "hmm_session"
SESSION_REFRESH = timedelta(minutes=5)
GITHUB_STALE = timedelta(hours=24)
ONBOARDED = frozenset({OnboardingStatus.COMPLETED, OnboardingStatus.REOPENED})
TELEGRAM_CODE_TTL = timedelta(minutes=10)


@dataclass
class AccountState:
    telegram: models.TelegramLink | None
    unread_count: int
    allowed_actions: list[AccountAction]


async def account_state(session: AsyncSession, actor: models.User, now: datetime) -> AccountState:
    link = await session.get(models.TelegramLink, actor.id)
    unread = await session.scalar(
        select(func.count()).where(
            models.Notification.user_id == actor.id, models.Notification.read_at.is_(None)
        )
    )
    restricted = set(
        await session.scalars(
            select(models.MemberRestriction.kind).where(
                models.MemberRestriction.user_id == actor.id,
                models.MemberRestriction.lifted_at.is_(None),
                (models.MemberRestriction.until.is_(None)) | (models.MemberRestriction.until > now),
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
    return AccountState(
        link, unread or 0, [action for action in AccountAction if action in allowed]
    )


def require_service_token(token: str | None, configured: str | None) -> None:
    if not configured or token is None:
        raise ProblemError(ErrorCode.UNAUTHENTICATED)
    if not hmac.compare_digest(token.encode(), configured.encode()):
        raise ProblemError(ErrorCode.UNAUTHENTICATED)


async def resolve_telegram_user(session: AsyncSession, telegram_user_id: str | None) -> models.User:
    if telegram_user_id is None:
        raise ProblemError(ErrorCode.TELEGRAM_NOT_LINKED)
    try:
        identity = int(telegram_user_id)
    except ValueError as error:
        raise ProblemError(ErrorCode.TELEGRAM_NOT_LINKED) from error
    if not -(2**63) <= identity < 2**63:
        raise ProblemError(ErrorCode.TELEGRAM_NOT_LINKED)
    user_id = await session.scalar(
        select(models.TelegramLink.user_id).where(models.TelegramLink.telegram_user_id == identity)
    )
    if user_id is None:
        raise ProblemError(ErrorCode.TELEGRAM_NOT_LINKED)
    actor = await lock_user(session, user_id)
    link = await session.scalar(
        select(models.TelegramLink)
        .where(
            models.TelegramLink.user_id == user_id,
            models.TelegramLink.telegram_user_id == identity,
        )
        .with_for_update()
    )
    if link is None:
        raise ProblemError(ErrorCode.TELEGRAM_NOT_LINKED)
    if actor.banned_at is not None:
        raise ProblemError(ErrorCode.BANNED)
    link.broken_at = None
    await session.flush()
    return actor


async def create_telegram_code(
    session: AsyncSession, user_id: UUID, now: datetime
) -> models.TelegramLinkCode:
    await lock_user(session, user_id)
    await session.execute(
        update(models.TelegramLinkCode)
        .where(
            models.TelegramLinkCode.user_id == user_id,
            models.TelegramLinkCode.used_at.is_(None),
            models.TelegramLinkCode.expires_at > now,
        )
        .values(expires_at=now)
    )
    while True:
        code = b32encode(secrets.token_bytes(5)).decode()
        record = await session.scalar(
            insert(models.TelegramLinkCode)
            .values(code=code, user_id=user_id, expires_at=now + TELEGRAM_CODE_TTL)
            .on_conflict_do_nothing(index_elements=[models.TelegramLinkCode.code])
            .returning(models.TelegramLinkCode)
        )
        if record is not None:
            return record


async def lock_telegram_account(session: AsyncSession, telegram_user_id: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": f"telegram:{telegram_user_id}"},
    )


async def link_telegram(
    session: AsyncSession,
    code: str,
    telegram_user_id: int,
    chat_id: int,
    username: str | None,
    now: datetime,
) -> models.User:
    user_id = await session.scalar(
        select(models.TelegramLinkCode.user_id).where(models.TelegramLinkCode.code == code)
    )
    if user_id is None:
        raise ProblemError(ErrorCode.CODE_INVALID)
    await lock_telegram_account(session, telegram_user_id)
    actor = await lock_user(session, user_id)
    record = await session.get(models.TelegramLinkCode, code, with_for_update=True)
    if record is None:
        raise ProblemError(ErrorCode.CODE_INVALID)
    if record.used_at is not None or record.expires_at <= now:
        raise ProblemError(ErrorCode.CODE_EXPIRED)
    owner = await session.scalar(
        select(models.TelegramLink)
        .where(models.TelegramLink.telegram_user_id == telegram_user_id)
        .with_for_update()
    )
    if owner is not None and owner.user_id != user_id:
        raise ProblemError(ErrorCode.TELEGRAM_ALREADY_LINKED)
    link = await session.get(models.TelegramLink, user_id, with_for_update=True)
    if link is None:
        link = models.TelegramLink(user_id=user_id)
        session.add(link)
    link.telegram_user_id = telegram_user_id
    link.chat_id = chat_id
    link.username = username
    link.linked_at = now
    link.broken_at = None
    record.used_at = now
    await session.flush()
    return actor


async def unlink_user_telegram(session: AsyncSession, user_id: UUID) -> None:
    await lock_user(session, user_id)
    await session.execute(delete(models.TelegramLink).where(models.TelegramLink.user_id == user_id))


async def unlink_telegram_account(session: AsyncSession, telegram_user_id: int) -> None:
    await lock_telegram_account(session, telegram_user_id)
    await session.execute(
        delete(models.TelegramLink).where(models.TelegramLink.telegram_user_id == telegram_user_id)
    )


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
    user: models.User,
    now: datetime,
    ttl: timedelta,
    user_agent: str | None,
    ip: str | None,
) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        models.UserSession(
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
) -> tuple[models.User, timedelta | None]:
    row = (
        await session.execute(
            select(models.UserSession, models.User)
            .join(models.User, models.User.id == models.UserSession.user_id)
            .where(models.UserSession.token_hash == hash_token(token))
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
    statement = delete(models.UserSession).where(models.UserSession.user_id == user_id)
    if token is not None:
        statement = statement.where(models.UserSession.token_hash == hash_token(token))
    await session.execute(statement)


async def _grant_bootstrap_admin(session: AsyncSession, user: models.User) -> None:
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
) -> models.User:
    listed = profile.login.lower() in admin_logins
    user = await session.scalar(
        select(models.User).where(models.User.github_id == profile.id).with_for_update()
    )
    if user is None:
        user = models.User(
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
        and not await session.scalar(select(exists().where(models.User.is_admin)))
    ):
        await _grant_bootstrap_admin(session, user)
    fetched_at = await session.scalar(
        select(models.GithubSnapshot.fetched_at).where(models.GithubSnapshot.user_id == user.id)
    )
    if fetched_at is None or now - fetched_at > GITHUB_STALE:
        await defer_job(session, jobs, JobName.GITHUB_SYNC, user_id=str(user.id))
    await session.flush()
    return user


def dnd_until(user: models.User) -> datetime | Literal["indefinite"] | None:
    return "indefinite" if user.dnd_indefinite else user.dnd_until


def dnd_active(user: models.User, now: datetime) -> bool:
    return user.dnd_indefinite or (user.dnd_until is not None and user.dnd_until > now)


async def lock_user(session: AsyncSession, user_id: UUID) -> models.User:
    user = await session.scalar(
        select(models.User)
        .where(models.User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return user


async def admin_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).where(models.User.is_admin)) or 0


async def refresh_user(session: AsyncSession, user: models.User) -> None:
    await session.flush()
    await session.refresh(user)


async def edit_user(
    session: AsyncSession,
    user: models.User,
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
    session: AsyncSession, user: models.User, value: bool, actor: UUID, reason: str | None
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
    session: AsyncSession,
    jobs: procrastinate.App,
    user: models.User,
    reason: str,
    actor: UUID,
    now: datetime,
) -> None:
    if user.banned_at is not None:
        return
    if user.is_admin:
        raise ProblemError(ErrorCode.INVALID_STATE, "Revoke admin before banning")
    user.banned_at = now
    user.ban_reason = reason
    await delete_sessions(session, user.id)
    from hmm_core.formation import service as formation

    await formation.withdraw_user_enrollments(session, user.id, now)
    await formation.withdraw_user_pending_items(session, user.id, now)
    from hmm_core.projects import service as projects

    await projects.remove_user_from_standalone_projects(
        session, user.id, actor, ActorKind.ADMIN, now
    )
    await formation.remove_user_from_teams(session, user.id, now, actor=actor)
    from hmm_core.mentorship.service import cancel_all_for_ban, withdraw_all_for_ban

    await withdraw_all_for_ban(session, user.id, now)
    await cancel_all_for_ban(session, jobs, user.id, now)
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
    await refresh_member_confirmations(session, jobs, user.id, now)


async def unban_user(
    session: AsyncSession,
    jobs: procrastinate.App,
    user: models.User,
    actor: UUID,
    reason: str | None,
    now: datetime,
) -> None:
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
    await refresh_member_confirmations(session, jobs, user.id, now)
