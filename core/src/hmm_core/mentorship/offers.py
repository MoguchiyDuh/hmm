from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import (
    ErrorCode,
    HelpOfferStatus,
    HelpRequestFormat,
    HelpRequestStatus,
    MentorshipStatus,
    RestrictionKind,
    ReviewContext,
    Trust,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    HelpOfferAcceptedPayload,
    HelpOfferReceivedPayload,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.timeline import hide_feed_subject
from hmm_core.identity.service import lock_user
from hmm_core.mentorship import schemas, views
from hmm_core.mentorship.mentor_offers import (
    active_learner_count,
    active_mentor_count,
    effective_area_score,
)
from hmm_core.mentorship.requests import LIVE_STATUSES, close_pending_offers, request_visible
from hmm_core.notifications.service import notify
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings
from hmm_core.scores.reputation import reputation_summaries
from hmm_core.scores.restrictions import require_unrestricted


async def create_help_offer(
    session: AsyncSession,
    actor: models.User,
    request_id: UUID,
    body: schemas.CreateHelpOfferBody,
    now: datetime,
) -> schemas.HelpOffer:
    actor = await lock_user(session, actor.id)
    request = await session.scalar(
        select(models.HelpRequest)
        .where(models.HelpRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None or not request_visible(request, actor.id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id == request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status not in LIVE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await require_unrestricted(session, actor.id, RestrictionKind.MENTORING, now)
    offer = await session.scalar(
        select(models.HelpOffer)
        .where(
            models.HelpOffer.request_id == request.id,
            models.HelpOffer.mentor_id == actor.id,
        )
        .with_for_update()
    )
    if offer is not None:
        if offer.status == HelpOfferStatus.PENDING.value:
            return (await views.help_offer_views(session, request, [offer], actor, now))[0]
        if offer.status == HelpOfferStatus.ACCEPTED.value:
            raise ProblemError(ErrorCode.INVALID_STATE)
    settings = await load_settings(session)
    if await active_mentor_count(session, actor.id) >= views.mentor_capacity(actor, settings):
        raise ProblemError(ErrorCode.MENTOR_CAPACITY_REACHED)
    if offer is None:
        offer = models.HelpOffer(
            request_id=request.id,
            mentor_id=actor.id,
            message=body.message,
            status=HelpOfferStatus.PENDING.value,
        )
        session.add(offer)
    else:
        offer.status = HelpOfferStatus.PENDING.value
        offer.message = body.message
    await session.flush()
    await session.refresh(offer)
    learner = await session.get(models.User, request.learner_id)
    area = await session.get(models.Area, request.area_id)
    assert learner is not None and area is not None
    mentor_score = await effective_area_score(session, actor.id, request.area_id)
    learner_score = await effective_area_score(session, request.learner_id, request.area_id)
    summary = (await reputation_summaries(session, [actor.id]))[actor.id][ReviewContext.MENTOR]
    past_count = await session.scalar(
        select(func.count())
        .select_from(models.Mentorship)
        .where(
            models.Mentorship.mentor_id == actor.id,
            models.Mentorship.status == MentorshipStatus.CLOSED.value,
        )
    )
    await notify(
        session,
        request.learner_id,
        HelpOfferReceivedPayload(
            help_request_id=request.id,
            help_offer_id=offer.id,
            learner=views.notification_person(learner),
            mentor=views.notification_person(actor),
            message=offer.message,
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            format=HelpRequestFormat(request.format),
            expires_at=request.expires_at,
            mentor_score=mentor_score,
            learner_score=learner_score,
            mentor_rating=summary.avg,
            mentor_review_count=summary.count,
            past_mentorship_count=int(past_count or 0),
            kind=views.classification(mentor_score, learner_score, settings),
            link=f"/help-requests/{request.id}",
            actions=(
                ApiAction(
                    kind="api",
                    key=ActionKey.HELP_OFFER_ACCEPT,
                    entity_id=offer.id,
                    label_key="notifications.actions.help_offer_accept",
                ),
            ),
        ),
        now,
        dedupe_key=f"help_offer_received:{offer.id}",
    )
    return (await views.help_offer_views(session, request, [offer], actor, now))[0]


async def withdraw_help_offer(
    session: AsyncSession, actor: models.User, offer_id: UUID, now: datetime
) -> None:
    offer = await session.scalar(
        select(models.HelpOffer).where(models.HelpOffer.id == offer_id).with_for_update()
    )
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    request = await session.get(models.HelpRequest, offer.request_id)
    if request is None or actor.id not in {offer.mentor_id, request.learner_id}:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id != offer.mentor_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if offer.status != HelpOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE)
    offer.status = HelpOfferStatus.WITHDRAWN.value
    await session.flush()


async def accept_help_offer(
    session: AsyncSession, actor: models.User, offer_id: UUID, now: datetime
) -> schemas.Mentorship:
    found = await session.get(models.HelpOffer, offer_id)
    if found is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    unlocked = await session.get(models.HelpRequest, found.request_id)
    if unlocked is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    ids = sorted((unlocked.learner_id, found.mentor_id), key=str)
    locked = {user_id: await lock_user(session, user_id) for user_id in ids}
    learner = locked[unlocked.learner_id]
    mentor = locked[found.mentor_id]
    request = await session.scalar(
        select(models.HelpRequest)
        .where(models.HelpRequest.id == found.request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None or not request_visible(request, actor.id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id != request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status not in LIVE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    offer = await session.scalar(
        select(models.HelpOffer)
        .where(models.HelpOffer.id == offer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if offer.status != HelpOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE)
    settings = await load_settings(session)
    if await active_mentor_count(session, mentor.id) >= views.mentor_capacity(mentor, settings):
        raise ProblemError(ErrorCode.MENTOR_CAPACITY_REACHED)
    if await active_learner_count(session, learner.id) >= settings.mentee_active_max:
        raise ProblemError(ErrorCode.MENTEE_LIMIT_REACHED)
    mentor_score = await effective_area_score(session, mentor.id, request.area_id)
    learner_score = await effective_area_score(session, learner.id, request.area_id)
    kind = views.classification(mentor_score, learner_score, settings)
    mentorship = models.Mentorship(
        request_id=request.id,
        mentor_id=mentor.id,
        learner_id=learner.id,
        area_id=request.area_id,
        kind=kind.value,
        mentor_score_at_start=Decimal(str(round(mentor_score, 2))),
        learner_score_at_start=Decimal(str(round(learner_score, 2))),
        status=MentorshipStatus.ACTIVE.value,
        started_at=now,
        mentor_trust=Trust.SELF.value,
        learner_trust=Trust.SELF.value,
    )
    session.add(mentorship)
    await session.flush()
    await session.refresh(mentorship)
    offer.status = HelpOfferStatus.ACCEPTED.value
    request.status = HelpRequestStatus.MATCHED.value
    await session.flush()
    await close_pending_offers(
        session, request, "The learner accepted another mentor's offer.", now
    )
    await hide_feed_subject(session, "help_request", request.id, now)
    area = await session.get(models.Area, request.area_id)
    assert area is not None
    await notify(
        session,
        mentor.id,
        HelpOfferAcceptedPayload(
            help_request_id=request.id,
            help_offer_id=offer.id,
            mentorship_id=mentorship.id,
            learner=views.notification_person(learner),
            mentor=views.notification_person(mentor),
            message=offer.message,
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            format=HelpRequestFormat(request.format),
            expires_at=request.expires_at,
            kind=kind,
            link=f"/mentorships/{mentorship.id}",
            actions=(),
        ),
        now,
        dedupe_key=f"help_offer_accepted:{offer.id}",
    )
    await session.flush()
    return await views.mentorship_view(session, mentorship, actor, now)
