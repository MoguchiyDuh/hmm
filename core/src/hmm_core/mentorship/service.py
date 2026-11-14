from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import procrastinate
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
    HelpOfferClosedPayload,
    HelpOfferReceivedPayload,
    HelpRequestDeclinedPayload,
    HelpRequestDirectPayload,
    HelpRequestExpiredPayload,
    LinkAction,
)
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.timeline import hide_feed_subject, record_help_request
from hmm_core.identity.service import ONBOARDED, dnd_active, lock_user
from hmm_core.mentorship import schemas, views
from hmm_core.notifications.service import notify
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.settings import load_settings
from hmm_core.scores.reputation import reputation_summaries
from hmm_core.scores.restrictions import require_unrestricted

LIVE_STATUSES = (HelpRequestStatus.OPEN.value, HelpRequestStatus.DIRECT.value)


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


def request_visible(request: models.HelpRequest, actor_id: UUID) -> bool:
    return request.status == HelpRequestStatus.OPEN.value or actor_id in {
        request.learner_id,
        request.target_mentor_id,
    }


async def create_help_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    body: schemas.CreateHelpRequestBody,
    now: datetime,
) -> schemas.HelpRequest:
    area = await load_area(session, body.area_key)
    learner = await lock_user(session, actor.id)
    settings = await load_settings(session)
    if await active_learner_count(session, actor.id) >= settings.mentee_active_max:
        raise ProblemError(ErrorCode.MENTEE_LIMIT_REACHED)
    live = list(
        await session.scalars(
            select(models.HelpRequest)
            .where(
                models.HelpRequest.learner_id == actor.id,
                models.HelpRequest.area_id == area.id,
                models.HelpRequest.status.in_(LIVE_STATUSES),
            )
            .with_for_update()
        )
    )
    for row in live:
        if row.expires_at <= now:
            await expire_request(session, row, now)
        else:
            raise ProblemError(ErrorCode.HELP_REQUEST_OPEN)
    target: models.User | None = None
    status = HelpRequestStatus.OPEN
    if body.target_mentor_id is not None:
        if body.target_mentor_id == actor.id:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, "You cannot target yourself")
        target = await session.get(models.User, body.target_mentor_id)
        if (
            target is None
            or target.banned_at is not None
            or target.onboarding_status not in ONBOARDED
        ):
            raise ProblemError(ErrorCode.NOT_FOUND)
        if dnd_active(target, now):
            raise ProblemError(ErrorCode.USER_DND)
        status = HelpRequestStatus.DIRECT
    request = models.HelpRequest(
        learner_id=actor.id,
        area_id=area.id,
        topic=body.topic,
        goal=body.goal,
        format=body.format.value,
        target_mentor_id=body.target_mentor_id,
        status=status.value,
        expires_at=now + timedelta(days=settings.help_request_ttl_days),
    )
    session.add(request)
    await session.flush()
    await session.refresh(request)
    if target is None:
        await record_help_request(session, request, actor.id, now)
        await defer_job(session, jobs, JobName.MATCH_MENTORS, request_id=str(request.id))
    else:
        await notify(
            session,
            target.id,
            HelpRequestDirectPayload(
                help_request_id=request.id,
                learner=views.notification_person(learner),
                area_key=area.key,
                area_name=area.name,
                topic=request.topic,
                goal=request.goal,
                format=HelpRequestFormat(request.format),
                expires_at=request.expires_at,
                respond_by=now + views.DIRECT_OPEN_AFTER,
                link=f"/help-requests/{request.id}",
                actions=(
                    ApiAction(
                        kind="api",
                        key=ActionKey.HELP_REQUEST_DECLINE,
                        entity_id=request.id,
                        label_key="notifications.actions.help_request_decline",
                    ),
                    LinkAction(
                        kind="link", label_key="notifications.actions.help", url="/mentorship"
                    ),
                ),
            ),
            now,
            dedupe_key=f"help_request_direct:{request.id}",
        )
    return await views.help_request_view(session, request, actor, now)


async def get_help_request(
    session: AsyncSession, actor: models.User, request_id: UUID, now: datetime
) -> schemas.HelpRequest:
    request = await session.get(models.HelpRequest, request_id)
    if request is None or not request_visible(request, actor.id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return await views.help_request_view(session, request, actor, now)


async def list_help_requests(
    session: AsyncSession,
    actor: models.User,
    pagination: CursorParams,
    *,
    area_key: str | None,
    request_status: HelpRequestStatus,
    now: datetime,
) -> schemas.HelpRequestPage:
    statement = select(models.HelpRequest).where(
        models.HelpRequest.status == request_status.value,
        or_(
            models.HelpRequest.status == HelpRequestStatus.OPEN.value,
            models.HelpRequest.learner_id == actor.id,
            models.HelpRequest.target_mentor_id == actor.id,
        ),
    )
    if area_key is not None:
        area = await load_area(session, area_key)
        statement = statement.where(models.HelpRequest.area_id == area.id)
    rows = list(
        await session.scalars(
            apply_keyset(
                statement,
                pagination,
                sort=[models.HelpRequest.created_at],
                id_column=models.HelpRequest.id,
                descending=True,
            )
        )
    )
    items = rows[: pagination.limit]
    next_cursor = (
        encode_cursor([items[-1].created_at, items[-1].id])
        if len(rows) > pagination.limit
        else None
    )
    return await views.help_request_page(session, items, next_cursor, actor, now)


async def update_help_request(
    session: AsyncSession,
    actor: models.User,
    request_id: UUID,
    body: schemas.UpdateHelpRequestBody,
    now: datetime,
) -> schemas.HelpRequest:
    request = await session.scalar(
        select(models.HelpRequest).where(models.HelpRequest.id == request_id).with_for_update()
    )
    if request is None or not request_visible(request, actor.id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id != request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status not in LIVE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if body.topic is not None:
        request.topic = body.topic
    if body.goal is not None:
        request.goal = body.goal
    if body.format is not None:
        request.format = body.format.value
    await session.flush()
    await session.refresh(request)
    return await views.help_request_view(session, request, actor, now)


async def close_pending_offers(
    session: AsyncSession, request: models.HelpRequest, reason: str, now: datetime
) -> None:
    offers = list(
        await session.scalars(
            select(models.HelpOffer)
            .where(
                models.HelpOffer.request_id == request.id,
                models.HelpOffer.status == HelpOfferStatus.PENDING.value,
            )
            .with_for_update()
        )
    )
    if not offers:
        return
    ids = {request.learner_id, *(offer.mentor_id for offer in offers)}
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(ids)))
    }
    area = await session.get(models.Area, request.area_id)
    assert area is not None
    learner = views.notification_person(users[request.learner_id])
    for offer in offers:
        offer.status = HelpOfferStatus.CLOSED.value
        await notify(
            session,
            offer.mentor_id,
            HelpOfferClosedPayload(
                help_request_id=request.id,
                help_offer_id=offer.id,
                learner=learner,
                mentor=views.notification_person(users[offer.mentor_id]),
                message=offer.message,
                area_key=area.key,
                area_name=area.name,
                topic=request.topic,
                goal=request.goal,
                format=HelpRequestFormat(request.format),
                expires_at=request.expires_at,
                reason=reason,
                link=f"/help-requests/{request.id}",
                actions=(),
            ),
            now,
            dedupe_key=f"help_offer_closed:{offer.id}",
        )
    await session.flush()


async def expire_request(session: AsyncSession, request: models.HelpRequest, now: datetime) -> None:
    request.status = HelpRequestStatus.EXPIRED.value
    await close_pending_offers(session, request, "The help request expired.", now)
    await hide_feed_subject(session, "help_request", request.id, now)
    learner = await session.get(models.User, request.learner_id)
    area = await session.get(models.Area, request.area_id)
    assert learner is not None and area is not None
    await notify(
        session,
        request.learner_id,
        HelpRequestExpiredPayload(
            help_request_id=request.id,
            learner=views.notification_person(learner),
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            format=HelpRequestFormat(request.format),
            expires_at=request.expires_at,
            link=f"/help-requests/{request.id}",
            actions=(
                LinkAction(
                    kind="link",
                    label_key="notifications.actions.reopen",
                    url=f"/mentorship/new?from={request.id}",
                ),
            ),
        ),
        now,
        dedupe_key=f"help_request_expired:{request.id}",
    )
    await session.flush()


async def expire_help_requests(session: AsyncSession, now: datetime, *, limit: int = 50) -> int:
    rows = list(
        await session.scalars(
            select(models.HelpRequest)
            .where(
                models.HelpRequest.status.in_(LIVE_STATUSES),
                models.HelpRequest.expires_at <= now,
            )
            .order_by(models.HelpRequest.expires_at, models.HelpRequest.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        await expire_request(session, row, now)
    return len(rows)


async def lock_visible_request(
    session: AsyncSession, actor: models.User, request_id: UUID
) -> models.HelpRequest:
    request = await session.scalar(
        select(models.HelpRequest).where(models.HelpRequest.id == request_id).with_for_update()
    )
    if request is None or not request_visible(request, actor.id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return request


async def open_to_everyone(
    session: AsyncSession, jobs: procrastinate.App, request: models.HelpRequest, now: datetime
) -> None:
    request.status = HelpRequestStatus.OPEN.value
    await session.flush()
    await record_help_request(session, request, request.learner_id, now)
    await defer_job(session, jobs, JobName.MATCH_MENTORS, request_id=str(request.id))
    await session.flush()
    await session.refresh(request)


async def cancel_help_request(
    session: AsyncSession, actor: models.User, request_id: UUID, now: datetime
) -> schemas.HelpRequest:
    request = await lock_visible_request(session, actor, request_id)
    if actor.id != request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == HelpRequestStatus.CANCELLED.value:
        return await views.help_request_view(session, request, actor, now)
    if request.status not in LIVE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    request.status = HelpRequestStatus.CANCELLED.value
    await close_pending_offers(session, request, "The learner cancelled the help request.", now)
    await hide_feed_subject(session, "help_request", request.id, now)
    await session.flush()
    await session.refresh(request)
    return await views.help_request_view(session, request, actor, now)


async def decline_help_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    request_id: UUID,
    now: datetime,
) -> schemas.HelpRequest:
    request = await lock_visible_request(session, actor, request_id)
    if actor.id != request.target_mentor_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status != HelpRequestStatus.DIRECT.value:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await open_to_everyone(session, jobs, request, now)
    learner = await session.get(models.User, request.learner_id)
    area = await session.get(models.Area, request.area_id)
    assert learner is not None and area is not None
    await notify(
        session,
        request.learner_id,
        HelpRequestDeclinedPayload(
            help_request_id=request.id,
            learner=views.notification_person(learner),
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            format=HelpRequestFormat(request.format),
            expires_at=request.expires_at,
            link="/mentorship",
            actions=(
                ApiAction(
                    kind="api",
                    key=ActionKey.HELP_REQUEST_OPEN,
                    entity_id=request.id,
                    label_key="notifications.actions.help_request_open",
                ),
            ),
        ),
        now,
        dedupe_key=f"help_request_declined:{request.id}",
    )
    return await views.help_request_view(session, request, actor, now)


async def open_help_request(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    request_id: UUID,
    now: datetime,
) -> schemas.HelpRequest:
    request = await lock_visible_request(session, actor, request_id)
    if actor.id != request.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if request.status == HelpRequestStatus.OPEN.value:
        return await views.help_request_view(session, request, actor, now)
    if request.status != HelpRequestStatus.DIRECT.value:
        raise ProblemError(ErrorCode.INVALID_STATE)
    if now < request.created_at + views.DIRECT_OPEN_AFTER:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "A direct request can be opened three days after creation"
        )
    await open_to_everyone(session, jobs, request, now)
    return await views.help_request_view(session, request, actor, now)


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
