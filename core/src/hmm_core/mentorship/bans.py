from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import HelpOfferStatus, HelpRequestStatus, MentorshipStatus
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.timeline import hide_feed_subject
from hmm_core.mentorship.mentorships import CLOSABLE_STATUSES
from hmm_core.mentorship.requests import LIVE_STATUSES, close_pending_offers
from hmm_core.platform.jobs import JobName, defer_job


async def withdraw_all_for_ban(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    rows = list(
        await session.scalars(
            select(models.HelpRequest)
            .where(
                models.HelpRequest.learner_id == user_id,
                models.HelpRequest.status.in_(LIVE_STATUSES),
            )
            .with_for_update()
        )
    )
    for row in rows:
        row.status = HelpRequestStatus.CANCELLED.value
        await close_pending_offers(session, row, "The learner was banned.", now)
        await hide_feed_subject(session, "help_request", row.id, now)
    await session.execute(
        update(models.HelpOffer)
        .where(
            models.HelpOffer.mentor_id == user_id,
            models.HelpOffer.status == HelpOfferStatus.PENDING.value,
        )
        .values(status=HelpOfferStatus.WITHDRAWN.value)
    )
    await session.execute(
        update(models.MentorOffer)
        .where(models.MentorOffer.user_id == user_id, models.MentorOffer.active.is_(True))
        .values(active=False)
    )
    await session.flush()


async def cancel_all_for_ban(
    session: AsyncSession, jobs: procrastinate.App, user_id: UUID, now: datetime
) -> None:
    rows = list(
        await session.scalars(
            select(models.Mentorship)
            .where(
                or_(
                    models.Mentorship.mentor_id == user_id,
                    models.Mentorship.learner_id == user_id,
                ),
                models.Mentorship.status.in_(CLOSABLE_STATUSES),
                models.Mentorship.outcome.is_(None),
            )
            .order_by(models.Mentorship.id)
            .with_for_update()
        )
    )
    for row in rows:
        row.status = MentorshipStatus.CANCELLED.value
        row.stalled_at = None
        other = row.learner_id if row.mentor_id == user_id else row.mentor_id
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(other))
    await session.flush()
