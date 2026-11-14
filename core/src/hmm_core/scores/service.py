from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import procrastinate
from hmm_contracts.algo import ScoreWithReason
from hmm_contracts.common import (
    ActorKind,
    AreaScoreEventKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    OverrideEvidence,
)
from hmm_contracts.notifications import ScoreChange, ScoreChangedPayload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED, lock_user
from hmm_core.notifications.service import notify
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.settings import load_settings
from hmm_core.scores import schemas
from hmm_core.scores.schemas import AreaScoreAction

CENT = Decimal("0.01")


def quantize(value: float) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def effective(score: models.AreaScore) -> Decimal:
    return score.override if score.override is not None else score.calculated


def effective_reason(score: models.AreaScore) -> str:
    return (
        score.override_reason
        if score.override is not None and score.override_reason is not None
        else score.calc_reason
    )


async def load_area(session: AsyncSession, key: str) -> models.Area:
    area = await session.scalar(select(models.Area).where(models.Area.key == key))
    if area is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if area.archived_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Area is archived")
    return area


@dataclass(frozen=True, slots=True, kw_only=True)
class AreaScoreView:
    area: models.Area
    score: models.AreaScore


def member_area_score(
    area: models.Area, score: models.AreaScore, *, actor: models.User
) -> schemas.MemberAreaScore:
    privileged = actor.is_admin or actor.id == score.user_id
    allowed: list[AreaScoreAction] = []
    if actor.is_admin:
        allowed.append(AreaScoreAction.OVERRIDE)
        if score.override is not None:
            allowed.append(AreaScoreAction.CLEAR_OVERRIDE)
    return schemas.MemberAreaScore(
        area_id=area.id,
        area_key=area.key,
        effective=float(effective(score)),
        reason=effective_reason(score),
        calculated=float(score.calculated) if privileged else None,
        calc_reason=score.calc_reason if privileged else None,
        override=float(score.override) if privileged and score.override is not None else None,
        override_by=score.override_by if privileged else None,
        override_reason=score.override_reason if privileged else None,
        override_evidence=OverrideEvidence(score.override_evidence)
        if privileged and score.override_evidence is not None
        else None,
        override_at=score.override_at if privileged else None,
        allowed_actions=allowed,
    )


async def require_member(session: AsyncSession, user_id: UUID) -> models.User:
    user = await session.get(models.User, user_id)
    if user is None or user.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return user


async def list_scores(
    session: AsyncSession, user_id: UUID, actor: models.User
) -> list[schemas.MemberAreaScore]:
    await require_member(session, user_id)
    rows = await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id == user_id, models.Area.archived_at.is_(None))
        .order_by(models.Area.sort)
    )
    return [member_area_score(area, score, actor=actor) for score, area in rows]


async def list_score_history(
    session: AsyncSession,
    user_id: UUID,
    actor: models.User,
    pagination: CursorParams,
    area_key: str | None,
) -> schemas.AreaScoreEventPage:
    await require_member(session, user_id)
    if not (actor.is_admin or actor.id == user_id):
        raise ProblemError(ErrorCode.FORBIDDEN)
    statement = select(models.AreaScoreEvent).where(models.AreaScoreEvent.user_id == user_id)
    if area_key is not None:
        area = await session.scalar(select(models.Area).where(models.Area.key == area_key))
        if area is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        statement = statement.where(models.AreaScoreEvent.area_id == area.id)
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.AreaScoreEvent.created_at],
        id_column=models.AreaScoreEvent.id,
        serialize=lambda row: schemas.AreaScoreEvent.model_validate(row),
    )
    return schemas.AreaScoreEventPage(items=page.items, next_cursor=page.next_cursor)


async def write_calculated_scores(
    session: AsyncSession,
    user_id: UUID,
    scores: Mapping[str, ScoreWithReason],
    *,
    areas: Sequence[models.Area],
    algo_version: str,
    now: datetime,
) -> list[ScoreChange]:
    settings = await load_settings(session)
    threshold = Decimal(str(settings.score_change_notify_delta))
    area_map = {area.key: area for area in areas if area.archived_at is None}
    existing = {
        row.area_id: row
        for row in await session.scalars(
            select(models.AreaScore).where(models.AreaScore.user_id == user_id).with_for_update()
        )
    }
    changes: list[ScoreChange] = []
    for area in sorted(area_map.values(), key=lambda area: area.sort):
        if area.key not in scores:
            continue
        value = quantize(scores[area.key].value)
        reason = scores[area.key].reason
        old = existing.get(area.id)
        before = effective(old) if old is not None else Decimal("0.00")
        after = old.override if old is not None and old.override is not None else value
        if old is None:
            session.add(
                models.AreaScore(
                    user_id=user_id, area_id=area.id, calculated=value, calc_reason=reason
                )
            )
        else:
            old.calculated = value
            old.calc_reason = reason
        if after != before:
            session.add(
                models.AreaScoreEvent(
                    user_id=user_id,
                    area_id=area.id,
                    kind=AreaScoreEventKind.CALCULATED,
                    old_effective=before,
                    new_effective=after,
                    reason=reason,
                    algo_version=algo_version,
                    actor_id=None,
                )
            )
            changes.append(
                ScoreChange(
                    area_key=area.key,
                    area_name=area.name,
                    previous_score=float(before),
                    score=float(after),
                    reason=reason,
                )
            )
    notified = [
        change
        for change in changes
        if abs(Decimal(str(change.score)) - Decimal(str(change.previous_score))) >= threshold
    ]
    if notified:
        await notify(
            session,
            user_id,
            ScoreChangedPayload(link=f"/users/{user_id}/scores", changes=tuple(notified)),
            now,
        )
    return changes


async def set_score_override(
    session: AsyncSession,
    jobs: procrastinate.App,
    user_id: UUID,
    area_key: str,
    value: float,
    reason: str,
    evidence: OverrideEvidence,
    actor: UUID,
    now: datetime,
) -> AreaScoreView:
    target = await lock_user(session, user_id)
    if target.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.NOT_FOUND)
    area = await load_area(session, area_key)
    row = await session.scalar(
        select(models.AreaScore)
        .where(models.AreaScore.user_id == user_id, models.AreaScore.area_id == area.id)
        .with_for_update()
    )
    if row is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "No calculated score for this area")
    before = effective(row)
    after = quantize(value)
    if (
        row.override is not None
        and row.override == after
        and row.override_reason == reason
        and row.override_evidence == evidence.value
    ):
        return AreaScoreView(area=area, score=row)
    previous_override = float(before) if row.override is not None else None
    row.override = after
    row.override_by = actor
    row.override_reason = reason
    row.override_evidence = evidence.value
    row.override_at = now
    if after != before:
        session.add(
            models.AreaScoreEvent(
                user_id=user_id,
                area_id=area.id,
                kind=AreaScoreEventKind.OVERRIDE_SET,
                old_effective=before,
                new_effective=after,
                reason=reason,
                algo_version=None,
                actor_id=actor,
            )
        )
        settings = await load_settings(session)
        if abs(after - before) >= Decimal(str(settings.score_change_notify_delta)):
            await notify(
                session,
                user_id,
                ScoreChangedPayload(
                    link=f"/users/{user_id}/scores",
                    changes=(
                        ScoreChange(
                            area_key=area.key,
                            area_name=area.name,
                            previous_score=float(before),
                            score=float(after),
                            reason=reason,
                        ),
                    ),
                ),
                now,
            )
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.SCORE_OVERRIDE_SET,
        entity_type=AuditEntityType.AREA_SCORE,
        entity_id=f"{user_id}:{area.id}",
        before={
            "calculated": float(row.calculated),
            "override": previous_override,
            "effective": float(before),
        },
        after={
            "calculated": float(row.calculated),
            "override": float(after),
            "effective": float(after),
        },
        reason=reason,
    )
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return AreaScoreView(area=area, score=row)


async def remove_score_override(
    session: AsyncSession,
    jobs: procrastinate.App,
    user_id: UUID,
    area_key: str,
    reason: str | None,
    actor: UUID,
    now: datetime,
) -> AreaScoreView:
    target = await lock_user(session, user_id)
    if target.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.NOT_FOUND)
    area = await load_area(session, area_key)
    row = await session.scalar(
        select(models.AreaScore)
        .where(models.AreaScore.user_id == user_id, models.AreaScore.area_id == area.id)
        .with_for_update()
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.override is None:
        return AreaScoreView(area=area, score=row)
    before = row.override
    after = row.calculated
    event_reason = reason or row.override_reason or "Override removed"
    row.override = None
    row.override_by = None
    row.override_reason = None
    row.override_evidence = None
    row.override_at = None
    session.add(
        models.AreaScoreEvent(
            user_id=user_id,
            area_id=area.id,
            kind=AreaScoreEventKind.OVERRIDE_REMOVED,
            old_effective=before,
            new_effective=after,
            reason=event_reason,
            algo_version=None,
            actor_id=actor,
        )
    )
    settings = await load_settings(session)
    if abs(after - before) >= Decimal(str(settings.score_change_notify_delta)):
        await notify(
            session,
            user_id,
            ScoreChangedPayload(
                link=f"/users/{user_id}/scores",
                changes=(
                    ScoreChange(
                        area_key=area.key,
                        area_name=area.name,
                        previous_score=float(before),
                        score=float(after),
                        reason=event_reason,
                    ),
                ),
            ),
            now,
        )
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.SCORE_OVERRIDE_REMOVE,
        entity_type=AuditEntityType.AREA_SCORE,
        entity_id=f"{user_id}:{area.id}",
        before={
            "calculated": float(row.calculated),
            "override": float(before),
            "effective": float(before),
        },
        after={"calculated": float(row.calculated), "override": None, "effective": float(after)},
        reason=reason,
    )
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return AreaScoreView(area=area, score=row)
