from datetime import datetime
from uuid import UUID

from hmm_contracts.common import ErrorCode, MentorshipStatus
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import lock_user
from hmm_core.mentorship import schemas, views
from hmm_core.platform.errors import ProblemError


async def load_area(session: AsyncSession, key: str) -> models.Area:
    area = await session.scalar(
        select(models.Area).where(models.Area.key == key, models.Area.archived_at.is_(None))
    )
    if area is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return area


async def mentor_offers_view(
    session: AsyncSession, user: models.User, now: datetime
) -> schemas.MentorOffers:
    rows = await session.execute(
        select(models.MentorOffer, models.Area)
        .join(models.Area, models.Area.id == models.MentorOffer.area_id)
        .where(models.MentorOffer.user_id == user.id, models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    )
    return schemas.MentorOffers(
        capacity=user.mentor_capacity,
        offers=[
            schemas.MentorOfferInput(area_key=area.key, topics=row.topics, active=row.active)
            for row, area in rows
        ],
        allowed_actions=[schemas.MentorOffersAction.EDIT],
    )


async def replace_mentor_offers(
    session: AsyncSession,
    user: models.User,
    body: schemas.ReplaceMentorOffersBody,
    now: datetime,
) -> schemas.MentorOffers:
    resolved: list[tuple[schemas.MentorOfferInput, models.Area]] = []
    seen: set[str] = set()
    for offer in body.offers:
        if offer.area_key in seen:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Duplicate area_key {offer.area_key}")
        seen.add(offer.area_key)
    for offer in body.offers:
        resolved.append((offer, await load_area(session, offer.area_key)))
    locked = await lock_user(session, user.id)
    area_ids = [area.id for _, area in resolved]
    await session.execute(
        delete(models.MentorOffer).where(
            models.MentorOffer.user_id == locked.id,
            models.MentorOffer.area_id.not_in(area_ids),
        )
    )
    for offer, area in resolved:
        row = await session.get(models.MentorOffer, (locked.id, area.id))
        if row is None:
            row = models.MentorOffer(user_id=locked.id, area_id=area.id)
            session.add(row)
        row.topics = offer.topics
        row.active = offer.active
    locked.mentor_capacity = body.capacity
    await session.flush()
    return await mentor_offers_view(session, locked, now)


async def effective_area_score(session: AsyncSession, user_id: UUID, area_id: UUID) -> float:
    row = await session.scalar(
        select(models.AreaScore).where(
            models.AreaScore.user_id == user_id, models.AreaScore.area_id == area_id
        )
    )
    return views.effective_score(row)


async def active_mentor_count(session: AsyncSession, user_id: UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(models.Mentorship)
        .where(
            models.Mentorship.mentor_id == user_id,
            models.Mentorship.status == MentorshipStatus.ACTIVE,
        )
    )
    return int(count or 0)


async def active_learner_count(session: AsyncSession, user_id: UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(models.Mentorship)
        .where(
            models.Mentorship.learner_id == user_id,
            models.Mentorship.status == MentorshipStatus.ACTIVE,
        )
    )
    return int(count or 0)
