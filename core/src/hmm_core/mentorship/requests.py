from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import ErrorCode, HelpOfferStatus, HelpRequestFormat, HelpRequestStatus
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    HelpOfferClosedPayload,
    HelpRequestDeclinedPayload,
    HelpRequestDirectPayload,
    HelpRequestExpiredPayload,
    LinkAction,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed.timeline import hide_feed_subject, record_help_request
from hmm_core.identity.service import ONBOARDED, dnd_active, lock_user
from hmm_core.mentorship import schemas, views
from hmm_core.mentorship.mentor_offers import active_learner_count, load_area
from hmm_core.notifications.service import notify
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.settings import load_settings

LIVE_STATUSES = (HelpRequestStatus.OPEN.value, HelpRequestStatus.DIRECT.value)


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
