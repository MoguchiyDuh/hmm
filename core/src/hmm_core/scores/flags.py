from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from hmm_contracts.algo import Flag as AlgoFlag
from hmm_contracts.common import FlagStatus, FlagSubjectType, Severity
from hmm_contracts.notifications import AlertKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import raise_alert

SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
REASON_LIMIT = 1000

OWNER_COLUMNS: dict[FlagSubjectType, tuple[Any, Any]] = {
    FlagSubjectType.ATTENDANCE: (models.EventAttendance.id, models.EventAttendance.user_id),
    FlagSubjectType.PROJECT_MEMBER: (models.ProjectMember.id, models.ProjectMember.user_id),
    FlagSubjectType.MENTORSHIP: (models.Mentorship.id, models.Mentorship.mentor_id),
    FlagSubjectType.CONFIRMATION: (models.Confirmation.id, models.Confirmation.subject_user_id),
    FlagSubjectType.ACTIVITY_ENTRY: (models.ActivityEntry.id, models.ActivityEntry.user_id),
    FlagSubjectType.REVIEW: (models.Review.id, models.Review.reviewer_id),
}


async def upsert_flag(
    session: AsyncSession, user_id: UUID, flag: AlgoFlag, *, algo_version: str
) -> tuple[models.Flag, bool]:
    live = await session.scalar(
        select(models.Flag)
        .where(
            models.Flag.subject_type == flag.subject_type.value,
            models.Flag.subject_id == flag.subject_id,
            models.Flag.status == FlagStatus.TO_CHECK,
        )
        .with_for_update()
    )
    if live is None:
        row = models.Flag(
            user_id=user_id,
            subject_type=flag.subject_type.value,
            subject_id=flag.subject_id,
            related_user_ids=sorted(set(flag.related_user_ids)),
            reason=flag.reason,
            severity=flag.severity.value,
            status=FlagStatus.TO_CHECK,
            algo_version=algo_version,
            penalty=None,
        )
        session.add(row)
        await session.flush()
        return row, True
    live.related_user_ids = sorted(set(live.related_user_ids) | set(flag.related_user_ids))
    if flag.reason not in live.reason.splitlines():
        merged = f"{live.reason}\n{flag.reason}"
        if len(merged) <= REASON_LIMIT:
            live.reason = merged
    if SEVERITY_RANK[flag.severity] > SEVERITY_RANK[Severity(live.severity)]:
        live.severity = flag.severity.value
    live.algo_version = algo_version
    await session.flush()
    return live, False


async def _raise_flag_alert(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    display_name = await session.scalar(
        select(models.User.display_name).where(models.User.id == user_id)
    )
    await raise_alert(
        session,
        AlertKind.SUSPICIOUS_ACTIVITY,
        now,
        title="Suspicious activity detected",
        message="New to-check flags for this member",
        link=f"/admin/flags?user={user_id}",
        subject_type="user",
        subject_id=user_id,
        subject_title=display_name or "",
        dedupe_key=f"suspicious_activity:{user_id}:{now.date().isoformat()}",
    )


async def apply_member_flags(
    session: AsyncSession,
    user_id: UUID,
    flags: Sequence[AlgoFlag],
    *,
    algo_version: str,
    now: datetime,
) -> list[models.Flag]:
    incoming = {(flag.subject_type, flag.subject_id): flag for flag in flags}
    inserted = False
    for flag in incoming.values():
        _, created = await upsert_flag(session, user_id, flag, algo_version=algo_version)
        inserted = inserted or created
    await session.flush()
    if inserted:
        await _raise_flag_alert(session, user_id, now)
    return list(
        await session.scalars(
            select(models.Flag)
            .where(models.Flag.user_id == user_id, models.Flag.status == FlagStatus.TO_CHECK)
            .order_by(models.Flag.created_at, models.Flag.id)
        )
    )


async def _flag_owners(
    session: AsyncSession, flags: Sequence[AlgoFlag]
) -> dict[tuple[FlagSubjectType, UUID], UUID]:
    owners: dict[tuple[FlagSubjectType, UUID], UUID] = {}
    for subject_type, (id_column, owner_column) in OWNER_COLUMNS.items():
        ids = sorted({flag.subject_id for flag in flags if flag.subject_type == subject_type})
        if not ids:
            continue
        rows = await session.execute(select(id_column, owner_column).where(id_column.in_(ids)))
        for subject_id, owner_id in rows:
            owners[(subject_type, subject_id)] = owner_id
    for flag in flags:
        if flag.subject_type == FlagSubjectType.PROFILE:
            owners[(flag.subject_type, flag.subject_id)] = flag.subject_id
        elif flag.subject_type == FlagSubjectType.RING and flag.related_user_ids:
            owners[(flag.subject_type, flag.subject_id)] = min(flag.related_user_ids)
    candidates = sorted(set(owners.values()))
    existing = (
        set(await session.scalars(select(models.User.id).where(models.User.id.in_(candidates))))
        if candidates
        else set()
    )
    return {key: owner for key, owner in owners.items() if owner in existing}


async def apply_community_flags(
    session: AsyncSession, flags: Sequence[AlgoFlag], *, algo_version: str, now: datetime
) -> list[models.Flag]:
    incoming = {(flag.subject_type, flag.subject_id): flag for flag in flags}
    ordered = sorted(
        incoming.values(), key=lambda flag: (flag.subject_type.value, str(flag.subject_id))
    )
    owners = await _flag_owners(session, ordered)
    rows: list[models.Flag] = []
    alerted: set[UUID] = set()
    for flag in ordered:
        owner = owners.get((flag.subject_type, flag.subject_id))
        if owner is None:
            continue
        row, created = await upsert_flag(session, owner, flag, algo_version=algo_version)
        rows.append(row)
        if created:
            alerted.add(owner)
    await session.flush()
    for user_id in sorted(alerted, key=str):
        await _raise_flag_alert(session, user_id, now)
    return rows
