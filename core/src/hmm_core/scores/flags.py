from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import Flag as AlgoFlag
from hmm_contracts.common import FlagStatus, Severity
from hmm_contracts.notifications import AlertKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import raise_alert

SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
REASON_LIMIT = 1000


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
            session.add(
                models.Flag(
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
            )
            inserted = True
            continue
        live.related_user_ids = sorted(set(live.related_user_ids) | set(flag.related_user_ids))
        if flag.reason not in live.reason.splitlines():
            merged = f"{live.reason}\n{flag.reason}"
            if len(merged) <= REASON_LIMIT:
                live.reason = merged
        if SEVERITY_RANK[flag.severity] > SEVERITY_RANK[Severity(live.severity)]:
            live.severity = flag.severity.value
        live.algo_version = algo_version
    await session.flush()
    if inserted:
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
    return list(
        await session.scalars(
            select(models.Flag)
            .where(models.Flag.user_id == user_id, models.Flag.status == FlagStatus.TO_CHECK)
            .order_by(models.Flag.created_at, models.Flag.id)
        )
    )
