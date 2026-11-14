import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from hmm_contracts.algo import AlgoSettings, Area, FactAward, MemberHistory
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED, lock_user
from hmm_core.platform.algo import call_algo
from hmm_core.platform.jobs import JobName, Runtime, defer_job
from hmm_core.platform.settings import load_settings
from hmm_core.scores.flags import apply_member_flags
from hmm_core.scores.history import load_member_history
from hmm_core.scores.service import write_calculated_scores


def input_hash(history: MemberHistory, algo_version: str, settings: AlgoSettings) -> str:
    payload = history.model_dump(mode="json")
    payload["xp"] = 0.0
    payload["activity"] = 0.0
    payload["area_scores"] = {}
    document = {
        "history": payload,
        "algo_version": algo_version,
        "settings": settings.model_dump(mode="json"),
    }
    return sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def recompute_member(session: AsyncSession, runtime: Runtime, user_id: UUID) -> bool:
    user = await session.get(models.User, user_id)
    if user is None or user.onboarding_status not in ONBOARDED:
        return False
    areas = list(
        await session.scalars(
            select(models.Area).where(models.Area.archived_at.is_(None)).order_by(models.Area.sort)
        )
    )
    now = runtime.clock.now()
    version = runtime.algorithm.version
    history = await load_member_history(session, user_id, now)
    settings = await load_settings(session)
    digest = input_hash(history, version, settings)
    existing = await session.scalar(
        select(models.MemberMetric).where(models.MemberMetric.user_id == user_id)
    )
    if existing is not None and existing.input_hash == digest and existing.algo_version == version:
        return False
    ctx = await runtime.algo_context(session)
    metrics = await call_algo(
        "compute_member",
        runtime.algorithm.compute_member(
            ctx, history, [Area.model_validate(area, from_attributes=True) for area in areas]
        ),
    )
    keys = {area.key for area in areas}
    if set(metrics.area_scores) != keys:
        raise ValueError("compute_member returned scores for the wrong set of areas")
    if any(not 0 <= score.value <= 10 for score in metrics.area_scores.values()):
        raise ValueError("compute_member returned an area score outside 0..10")
    if metrics.xp < 0 or metrics.activity < 0:
        raise ValueError("compute_member returned negative xp or activity")
    facts = {(fact.type, fact.id) for fact in history.facts}
    awards: dict[tuple[str, UUID], FactAward] = {}
    for award in metrics.fact_awards:
        if (award.fact_type, award.fact_id) not in facts:
            raise ValueError("compute_member awarded an unknown fact")
        if award.xp < 0 or award.activity < 0:
            raise ValueError("compute_member returned a negative fact award")
        awards[(award.fact_type.value, award.fact_id)] = award
    await lock_user(session, user_id)
    await write_calculated_scores(
        session, user_id, metrics.area_scores, areas=areas, algo_version=version, now=now
    )
    await session.execute(delete(models.FactAward).where(models.FactAward.user_id == user_id))
    session.add_all(
        models.FactAward(
            fact_type=award.fact_type.value,
            fact_id=award.fact_id,
            user_id=user_id,
            xp=Decimal(str(award.xp)),
            activity=Decimal(str(award.activity)),
            reason=award.reason,
            algo_version=version,
            computed_at=now,
        )
        for award in awards.values()
    )
    values = {
        "xp": Decimal(str(metrics.xp)),
        "activity": Decimal(str(metrics.activity)),
        "algo_version": version,
        "input_hash": digest,
        "computed_at": now,
    }
    statement = insert(models.MemberMetric).values(user_id=user_id, **values)
    await session.execute(
        statement.on_conflict_do_update(index_elements=[models.MemberMetric.user_id], set_=values)
    )
    await apply_member_flags(session, user_id, metrics.flags, algo_version=version, now=now)
    await defer_job(
        session, runtime.jobs, JobName.COMPUTE_LEAD_FITS_FOR_MEMBER, user_id=str(user_id)
    )
    return True


async def recompute_all_members(session: AsyncSession, runtime: Runtime) -> int:
    ids = list(
        await session.scalars(
            select(models.User.id)
            .where(
                models.User.onboarding_status.in_(ONBOARDED),
                models.User.banned_at.is_(None),
            )
            .order_by(models.User.id)
        )
    )
    count = 0
    for user_id in ids:
        job_id = await defer_job(
            session, runtime.jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id)
        )
        if job_id is not None:
            count += 1
    return count
