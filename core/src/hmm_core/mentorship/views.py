from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.common import (
    HelpOfferStatus,
    HelpRequestFormat,
    HelpRequestStatus,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    ReviewContext,
    Trust,
)
from hmm_contracts.notifications import NotificationPerson
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.mentorship import schemas
from hmm_core.platform.schemas import UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.scores.reputation import empty_reputation, reputation_summaries

DIRECT_OPEN_AFTER = timedelta(days=3)

LIVE_REQUEST = frozenset({HelpRequestStatus.OPEN, HelpRequestStatus.DIRECT})


def user_ref(user: models.User) -> UserRef:
    return UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url)


def notification_person(user: models.User) -> NotificationPerson:
    return NotificationPerson(user_id=user.id, display_name=user.display_name)


def effective_score(row: models.AreaScore | None) -> float:
    if row is None:
        return 0.0
    return float(row.override if row.override is not None else row.calculated)


def mentor_capacity(user: models.User, settings: RuntimeSettings) -> int:
    if user.mentor_capacity is not None:
        return user.mentor_capacity
    return settings.mentor_capacity_default


def classification(
    mentor_score: float, learner_score: float, settings: RuntimeSettings
) -> MentorshipKind:
    if (
        mentor_score >= settings.mentor_min_score
        and mentor_score - learner_score >= settings.mentor_min_gap
    ):
        return MentorshipKind.MENTORING
    return MentorshipKind.STUDY_PAIR


def help_request_actions(
    request: models.HelpRequest, actor_id: UUID, now: datetime
) -> list[schemas.HelpRequestAction]:
    actions: set[schemas.HelpRequestAction] = set()
    if actor_id == request.learner_id:
        if request.status in LIVE_REQUEST:
            actions |= {schemas.HelpRequestAction.EDIT, schemas.HelpRequestAction.CANCEL}
            if (
                request.status == HelpRequestStatus.DIRECT
                and now >= request.created_at + DIRECT_OPEN_AFTER
            ):
                actions.add(schemas.HelpRequestAction.OPEN)
    elif actor_id == request.target_mentor_id:
        if request.status == HelpRequestStatus.DIRECT:
            actions |= {schemas.HelpRequestAction.DECLINE, schemas.HelpRequestAction.OFFER_HELP}
    elif request.status == HelpRequestStatus.OPEN:
        actions.add(schemas.HelpRequestAction.OFFER_HELP)
    return [action for action in schemas.HelpRequestAction if action in actions]


def help_offer_actions(
    offer: models.HelpOffer, request: models.HelpRequest, actor_id: UUID
) -> list[schemas.HelpOfferAction]:
    actions: set[schemas.HelpOfferAction] = set()
    pending = offer.status == HelpOfferStatus.PENDING
    if offer.mentor_id == actor_id and pending:
        actions.add(schemas.HelpOfferAction.WITHDRAW)
    if request.learner_id == actor_id and pending and request.status in LIVE_REQUEST:
        actions.add(schemas.HelpOfferAction.ACCEPT)
    return [action for action in schemas.HelpOfferAction if action in actions]


async def help_offer_views(
    session: AsyncSession,
    request: models.HelpRequest,
    offers: Sequence[models.HelpOffer],
    actor: models.User,
    now: datetime,
) -> list[schemas.HelpOffer]:
    ids = [offer.mentor_id for offer in offers]
    if not ids:
        return []
    users = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(ids)))
    }
    mentor_scores = {
        row.user_id: row
        for row in await session.scalars(
            select(models.AreaScore).where(
                models.AreaScore.user_id.in_(ids),
                models.AreaScore.area_id == request.area_id,
            )
        )
    }
    reputation = await reputation_summaries(session, ids)
    closed_counts: dict[UUID, int] = {
        mentor_id: int(count)
        for mentor_id, count in await session.execute(
            select(models.Mentorship.mentor_id, func.count())
            .where(
                models.Mentorship.mentor_id.in_(ids),
                models.Mentorship.status == MentorshipStatus.CLOSED,
            )
            .group_by(models.Mentorship.mentor_id)
        )
    }
    learner_row = await session.scalar(
        select(models.AreaScore).where(
            models.AreaScore.user_id == request.learner_id,
            models.AreaScore.area_id == request.area_id,
        )
    )
    learner_score = effective_score(learner_row)
    settings = await load_settings(session)
    views: list[schemas.HelpOffer] = []
    for offer in offers:
        mentor_score = effective_score(mentor_scores.get(offer.mentor_id))
        summary = reputation.get(offer.mentor_id, empty_reputation())[ReviewContext.MENTOR]
        views.append(
            schemas.HelpOffer(
                id=offer.id,
                request_id=offer.request_id,
                mentor_id=offer.mentor_id,
                mentor=user_ref(users[offer.mentor_id]),
                message=offer.message,
                status=HelpOfferStatus(offer.status),
                mentor_score=mentor_score,
                mentor_rating=summary.avg,
                mentor_review_count=summary.count,
                past_mentorship_count=closed_counts.get(offer.mentor_id, 0),
                kind=classification(mentor_score, learner_score, settings),
                created_at=offer.created_at,
                updated_at=offer.updated_at,
                allowed_actions=help_offer_actions(offer, request, actor.id),
            )
        )
    return views


async def help_request_view(
    session: AsyncSession, request: models.HelpRequest, actor: models.User, now: datetime
) -> schemas.HelpRequest:
    learner = await session.get(models.User, request.learner_id)
    assert learner is not None
    area_key = await session.scalar(
        select(models.Area.key).where(models.Area.id == request.area_id)
    )
    assert area_key is not None
    offers: list[schemas.HelpOffer] = []
    if actor.id == request.learner_id:
        rows = (
            await session.scalars(
                select(models.HelpOffer)
                .where(models.HelpOffer.request_id == request.id)
                .order_by(models.HelpOffer.created_at, models.HelpOffer.id)
            )
        ).all()
        offers = await help_offer_views(session, request, rows, actor, now)
    return schemas.HelpRequest(
        id=request.id,
        learner_id=request.learner_id,
        learner=user_ref(learner),
        area_key=area_key,
        topic=request.topic,
        goal=request.goal,
        format=HelpRequestFormat(request.format),
        target_mentor_id=request.target_mentor_id,
        status=HelpRequestStatus(request.status),
        expires_at=request.expires_at,
        offers=offers,
        created_at=request.created_at,
        updated_at=request.updated_at,
        allowed_actions=help_request_actions(request, actor.id, now),
    )


async def help_request_page(
    session: AsyncSession,
    requests: Sequence[models.HelpRequest],
    next_cursor: str | None,
    actor: models.User,
    now: datetime,
) -> schemas.HelpRequestPage:
    return schemas.HelpRequestPage(
        items=[await help_request_view(session, request, actor, now) for request in requests],
        next_cursor=next_cursor,
    )


def mentorship_actions(
    mentorship: models.Mentorship, actor_id: UUID
) -> list[schemas.MentorshipAction]:
    actions: set[schemas.MentorshipAction] = set()
    is_mentor = actor_id == mentorship.mentor_id
    is_learner = actor_id == mentorship.learner_id
    if is_mentor or is_learner:
        if mentorship.status == MentorshipStatus.ACTIVE:
            actions.add(schemas.MentorshipAction.CHECK_IN)
            if mentorship.last_checkin_at is None:
                actions.add(schemas.MentorshipAction.CANCEL)
            if is_learner:
                actions.add(schemas.MentorshipAction.CLOSE)
            if is_mentor:
                actions.add(schemas.MentorshipAction.PROPOSE_CLOSE)
        elif mentorship.status == MentorshipStatus.CLOSING and is_learner:
            actions.add(schemas.MentorshipAction.CLOSE)
    if (
        is_mentor
        and mentorship.status == MentorshipStatus.CLOSED
        and mentorship.outcome is not None
        and mentorship.mentor_acknowledged_at is None
    ):
        actions.add(schemas.MentorshipAction.ACKNOWLEDGE)
    return [action for action in schemas.MentorshipAction if action in actions]


async def mentorship_view(
    session: AsyncSession, mentorship: models.Mentorship, actor: models.User, now: datetime
) -> schemas.Mentorship:
    mentor = await session.get(models.User, mentorship.mentor_id)
    learner = await session.get(models.User, mentorship.learner_id)
    area = await session.get(models.Area, mentorship.area_id)
    assert mentor is not None and learner is not None and area is not None
    checkins = await session.scalars(
        select(models.MentorshipCheckin)
        .where(models.MentorshipCheckin.mentorship_id == mentorship.id)
        .order_by(
            models.MentorshipCheckin.occurred_on,
            models.MentorshipCheckin.created_at,
            models.MentorshipCheckin.id,
        )
    )
    return schemas.Mentorship(
        id=mentorship.id,
        request_id=mentorship.request_id,
        mentor_id=mentorship.mentor_id,
        learner_id=mentorship.learner_id,
        mentor=user_ref(mentor),
        learner=user_ref(learner),
        area_key=area.key,
        kind=MentorshipKind(mentorship.kind),
        mentor_score_at_start=float(mentorship.mentor_score_at_start),
        learner_score_at_start=float(mentorship.learner_score_at_start),
        status=MentorshipStatus(mentorship.status),
        started_at=mentorship.started_at,
        last_checkin_at=mentorship.last_checkin_at,
        close_proposed_at=mentorship.close_proposed_at,
        closed_at=mentorship.closed_at,
        outcome=MentorshipOutcome(mentorship.outcome) if mentorship.outcome is not None else None,
        summary=mentorship.summary,
        mentor_acknowledged_at=mentorship.mentor_acknowledged_at,
        endorsement=mentorship.endorsement,
        mentor_trust=Trust(mentorship.mentor_trust),
        learner_trust=Trust(mentorship.learner_trust),
        checkins=[
            schemas.MentorshipCheckin(
                id=checkin.id,
                mentorship_id=checkin.mentorship_id,
                author_id=checkin.author_id,
                note=checkin.note,
                occurred_on=checkin.occurred_on,
                minutes=checkin.minutes,
                created_at=checkin.created_at,
            )
            for checkin in checkins
        ],
        created_at=mentorship.created_at,
        updated_at=mentorship.updated_at,
        allowed_actions=mentorship_actions(mentorship, actor.id),
    )
