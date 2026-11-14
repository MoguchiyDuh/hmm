from collections.abc import Sequence
from uuid import UUID

from hmm_contracts.algo import RatingSummary
from hmm_contracts.common import ReviewContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models

HOST_MIN_RATINGS = 3
CONTEXTS = (ReviewContext.MEMBER, ReviewContext.LEAD, ReviewContext.MENTOR, ReviewContext.HOST)

type Reputation = dict[ReviewContext, RatingSummary]


def empty_reputation() -> Reputation:
    return {context: RatingSummary(avg=None, count=0) for context in CONTEXTS}


async def reputation_summaries(
    session: AsyncSession, user_ids: Sequence[UUID]
) -> dict[UUID, Reputation]:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(
            models.Review.reviewee_id,
            models.Review.context,
            func.sum(models.Review.rating),
            func.count(models.Review.rating),
        )
        .where(
            models.Review.reviewee_id.in_(ids),
            models.Review.rating.is_not(None),
            models.Review.revealed_at.is_not(None),
            models.Review.removed_at.is_(None),
        )
        .group_by(models.Review.reviewee_id, models.Review.context)
    )
    totals: dict[UUID, dict[ReviewContext, tuple[int, int]]] = {}
    for user_id, raw_context, total, count in rows:
        context = ReviewContext(raw_context)
        if context == ReviewContext.SPEAKER:
            context = ReviewContext.HOST
        bucket = totals.setdefault(user_id, {})
        previous_total, previous_count = bucket.get(context, (0, 0))
        bucket[context] = (previous_total + int(total or 0), previous_count + int(count))
    result: dict[UUID, Reputation] = {}
    for user_id in ids:
        reputation = empty_reputation()
        for context, (total, count) in totals.get(user_id, {}).items():
            visible = count > 0 and (context != ReviewContext.HOST or count >= HOST_MIN_RATINGS)
            reputation[context] = RatingSummary(
                avg=round(total / count, 2) if visible else None, count=count
            )
        result[user_id] = reputation
    return result


async def reputation_summary(session: AsyncSession, user_id: UUID) -> Reputation:
    return (await reputation_summaries(session, [user_id]))[user_id]
