from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    RestrictionKind,
)
from hmm_contracts.notifications import RestrictionLiftedPayload
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.confirmations import refresh_member_confirmations
from hmm_core.notifications.service import notify
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.scores import schemas
from hmm_core.scores.schemas import RestrictionAction


def _active_predicate(now: datetime) -> ColumnElement[bool]:
    return and_(
        models.MemberRestriction.lifted_at.is_(None),
        or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
    )


def _is_active(row: models.MemberRestriction, now: datetime) -> bool:
    return row.lifted_at is None and (row.until is None or row.until > now)


async def active_restrictions(
    session: AsyncSession, user_id: UUID, now: datetime
) -> set[RestrictionKind]:
    values = await session.scalars(
        select(models.MemberRestriction.kind).where(
            models.MemberRestriction.user_id == user_id, _active_predicate(now)
        )
    )
    return {RestrictionKind(value) for value in values}


async def require_unrestricted(
    session: AsyncSession, user_id: UUID, kind: RestrictionKind, now: datetime
) -> None:
    if kind in await active_restrictions(session, user_id, now):
        raise ProblemError(ErrorCode.RESTRICTED, f"{kind.value} restriction")


async def apply_restriction(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    user_id: UUID,
    kind: RestrictionKind,
    until: datetime | None,
    reason: str,
    flag_id: UUID | None,
    actor: UUID,
    now: datetime,
) -> models.MemberRestriction:
    row = models.MemberRestriction(
        user_id=user_id,
        kind=kind.value,
        until=until,
        reason=reason,
        flag_id=flag_id,
        created_by=actor,
        lifted_at=None,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.RESTRICTION_APPLY,
        entity_type=AuditEntityType.RESTRICTION,
        entity_id=row.id,
        before=None,
        after={
            "user_id": str(user_id),
            "kind": kind.value,
            "until": None if until is None else until.isoformat(),
            "flag_id": None if flag_id is None else str(flag_id),
            "reason": reason,
        },
        reason=reason,
    )
    await refresh_member_confirmations(session, jobs, user_id, now)
    return row


async def lift_restriction(
    session: AsyncSession,
    jobs: procrastinate.App,
    restriction_id: UUID,
    actor: UUID,
    reason: str,
    now: datetime,
) -> models.MemberRestriction:
    row = await session.scalar(
        select(models.MemberRestriction)
        .where(models.MemberRestriction.id == restriction_id)
        .with_for_update()
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.lifted_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Restriction is already lifted")
    if row.until is not None and row.until <= now:
        raise ProblemError(ErrorCode.INVALID_STATE, "Restriction has expired")
    row.lifted_at = now
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.RESTRICTION_LIFT,
        entity_type=AuditEntityType.RESTRICTION,
        entity_id=row.id,
        before={"lifted_at": None},
        after={"lifted_at": now.isoformat()},
        reason=reason,
    )
    await notify(
        session,
        row.user_id,
        RestrictionLiftedPayload(
            link=f"/users/{row.user_id}",
            restriction_id=row.id,
            restriction_kind=RestrictionKind(row.kind),
            reason=reason,
        ),
        now,
    )
    await refresh_member_confirmations(session, jobs, row.user_id, now)
    return row


async def list_restrictions(
    session: AsyncSession,
    *,
    user_id: UUID | None,
    active: bool | None,
    now: datetime,
) -> list[models.MemberRestriction]:
    query = select(models.MemberRestriction)
    if user_id is not None:
        query = query.where(models.MemberRestriction.user_id == user_id)
    if active is True:
        query = query.where(_active_predicate(now))
    elif active is False:
        query = query.where(
            or_(
                models.MemberRestriction.lifted_at.is_not(None),
                and_(
                    models.MemberRestriction.until.is_not(None),
                    models.MemberRestriction.until <= now,
                ),
            )
        )
    query = query.order_by(
        models.MemberRestriction.created_at.desc(), models.MemberRestriction.id.desc()
    )
    return list(await session.scalars(query))


def restriction_view(row: models.MemberRestriction, now: datetime) -> schemas.Restriction:
    return schemas.Restriction(
        id=row.id,
        user_id=row.user_id,
        kind=RestrictionKind(row.kind),
        until=row.until,
        reason=row.reason,
        flag_id=row.flag_id,
        created_by=row.created_by,
        lifted_at=row.lifted_at,
        created_at=row.created_at,
        allowed_actions=[RestrictionAction.LIFT] if _is_active(row, now) else [],
    )
