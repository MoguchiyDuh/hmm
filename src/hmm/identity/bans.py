from datetime import datetime
from uuid import UUID

import procrastinate
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ActorKind, AuditAction, AuditEntityType, ErrorCode
from hmm.feed.confirmations import refresh_member_confirmations
from hmm.formation import bans as formation_bans
from hmm.identity.service import delete_sessions
from hmm.mentorship import bans as mentorship_bans
from hmm.platform.audit import record_audit
from hmm.platform.errors import ProblemError
from hmm.projects import bans as projects_bans


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
    admin = await session.get(models.User, actor)
    await formation_bans.remove_user_from_formation(session, user.id, now, actor=admin)
    await projects_bans.remove_user_from_standalone_projects(
        session, user.id, actor, ActorKind.ADMIN, now
    )
    await mentorship_bans.withdraw_all_for_ban(session, user.id, now)
    await mentorship_bans.cancel_all_for_ban(session, jobs, user.id)
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
