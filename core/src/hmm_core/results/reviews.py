from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import ReviewIn
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    ErrorCode,
    FlagStatus,
    FlagSubjectType,
    MentorshipKind,
    ReviewContext,
    ReviewSubjectType,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.identity.service import ONBOARDED
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import UserRef
from hmm_core.results.schemas import Review, ReviewAction, ReviewRatingSummary
from hmm_core.results.windows import subject_details, window_participants
from hmm_core.scores.reputation import reputation_summary


def user_ref(user: models.User) -> UserRef:
    return UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url)


def build_review(
    review: models.Review, reviewer: models.User | None, allowed_actions: Sequence[ReviewAction]
) -> Review:
    return Review(
        id=review.id,
        window_id=review.window_id,
        subject_type=ReviewSubjectType(review.subject_type),
        subject_id=review.subject_id,
        reviewer=user_ref(reviewer) if reviewer is not None else None,
        reviewee_id=review.reviewee_id,
        context=ReviewContext(review.context),
        rating=review.rating,
        comment=review.comment,
        revealed_at=review.revealed_at,
        created_at=review.created_at,
        updated_at=review.updated_at,
        removed_at=review.removed_at,
        removed_by=review.removed_by,
        removed_reason=review.removed_reason,
        allowed_actions=list(allowed_actions),
    )


async def submit_review(
    session: AsyncSession,
    *,
    actor_id: UUID,
    window_id: UUID,
    reviewee_id: UUID,
    rating: int | None,
    comment: str,
    now: datetime,
) -> models.Review:
    window = await session.scalar(
        select(models.ReviewWindow).where(models.ReviewWindow.id == window_id).with_for_update()
    )
    if window is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if window.revealed_at is not None or now < window.opens_at or now >= window.closes_at:
        raise ProblemError(ErrorCode.REVIEW_WINDOW_CLOSED)
    participants = window_participants(window)
    if actor_id not in participants or reviewee_id not in participants:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if reviewee_id == actor_id:
        raise ProblemError(ErrorCode.FORBIDDEN, "You cannot review yourself")
    context = participants[reviewee_id]
    endorsement = False
    if window.subject_type == ReviewSubjectType.MENTORSHIP:
        mentorship = await session.get(models.Mentorship, window.subject_id)
        if mentorship is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        endorsement = (
            mentorship.kind == MentorshipKind.MENTORING and reviewee_id == mentorship.learner_id
        )
    if endorsement:
        if rating is not None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, "Endorsements have no rating")
    elif rating is None:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "A rating is required")
    text = comment.strip()
    if not text:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Comment must not be empty")
    existing = await session.scalar(
        select(models.Review)
        .where(
            models.Review.subject_type == window.subject_type,
            models.Review.subject_id == window.subject_id,
            models.Review.window_id == window.id,
            models.Review.reviewer_id == actor_id,
            models.Review.reviewee_id == reviewee_id,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.removed_at is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "Review was removed")
        existing.rating = rating
        existing.comment = text
        review = existing
    else:
        review = models.Review(
            window_id=window.id,
            subject_type=window.subject_type,
            subject_id=window.subject_id,
            reviewer_id=actor_id,
            reviewee_id=reviewee_id,
            context=context,
            rating=rating,
            comment=text,
        )
        session.add(review)
    await session.flush()
    if existing is not None:
        await session.refresh(review, ["updated_at"])
    return review


@dataclass(frozen=True)
class PendingWindow:
    window: models.ReviewWindow
    subject_title: str
    to_review: list[models.User]
    submitted: list[models.Review]


async def pending_windows(
    session: AsyncSession, *, actor_id: UUID, now: datetime
) -> list[PendingWindow]:
    windows = list(
        await session.scalars(
            select(models.ReviewWindow)
            .where(
                models.ReviewWindow.revealed_at.is_(None),
                models.ReviewWindow.opens_at <= now,
                models.ReviewWindow.closes_at > now,
            )
            .order_by(models.ReviewWindow.closes_at)
        )
    )
    if not windows:
        return []
    reviews = await session.scalars(
        select(models.Review)
        .where(
            models.Review.window_id.in_([window.id for window in windows]),
            models.Review.reviewer_id == actor_id,
        )
        .order_by(models.Review.created_at)
    )
    submitted: dict[UUID, list[models.Review]] = {}
    for review in reviews:
        if review.window_id is not None:
            submitted.setdefault(review.window_id, []).append(review)
    visible = [
        (window, participants)
        for window in windows
        if actor_id in (participants := window_participants(window))
    ]
    if not visible:
        return []
    user_ids = {user_id for _, participants in visible for user_id in participants}
    users = sorted(
        await session.scalars(select(models.User).where(models.User.id.in_(user_ids))),
        key=lambda user: (user.display_name, str(user.id)),
    )
    pending: list[PendingWindow] = []
    for window, participants in visible:
        own_reviews = submitted.get(window.id, [])
        reviewed = {review.reviewee_id for review in own_reviews}
        title, _ = await subject_details(
            session,
            subject_type=ReviewSubjectType(window.subject_type),
            subject_id=window.subject_id,
        )
        pending.append(
            PendingWindow(
                window=window,
                subject_title=title,
                to_review=[
                    user
                    for user in users
                    if user.id in participants and user.id != actor_id and user.id not in reviewed
                ],
                submitted=own_reviews,
            )
        )
    return pending


async def submit_host_review(
    session: AsyncSession,
    *,
    actor_id: UUID,
    event_id: UUID,
    reviewee_id: UUID,
    rating: int,
    comment: str,
    now: datetime,
) -> models.Review:
    event = await session.get(models.Event, event_id)
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if now < event.starts_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "Host reviews open when the event starts")
    reviewer_attendance = await session.scalar(
        select(models.EventAttendance).where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == actor_id,
        )
    )
    if reviewer_attendance is None or reviewer_attendance.status != AttendanceStatus.ATTENDED:
        raise ProblemError(ErrorCode.FORBIDDEN, "Only attendees who attended can rate")
    target_attendance = await session.scalar(
        select(models.EventAttendance).where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == reviewee_id,
        )
    )
    if (
        target_attendance is None
        or target_attendance.role not in {AttendanceRole.HOST, AttendanceRole.SPEAKER}
        or target_attendance.status == AttendanceStatus.CANCELLED
    ):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if reviewee_id == actor_id:
        raise ProblemError(ErrorCode.FORBIDDEN, "You cannot review yourself")
    context = (
        ReviewContext.HOST
        if target_attendance.role == AttendanceRole.HOST
        else ReviewContext.SPEAKER
    )
    text = comment.strip()
    if not text:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Comment must not be empty")
    existing = await session.scalar(
        select(models.Review)
        .where(
            models.Review.subject_type == ReviewSubjectType.EVENT,
            models.Review.subject_id == event_id,
            models.Review.window_id.is_(None),
            models.Review.reviewer_id == actor_id,
            models.Review.reviewee_id == reviewee_id,
        )
        .with_for_update()
    )
    if existing is not None:
        existing.rating = rating
        existing.comment = text
        review = existing
    else:
        review = models.Review(
            window_id=None,
            subject_type=ReviewSubjectType.EVENT,
            subject_id=event_id,
            reviewer_id=actor_id,
            reviewee_id=reviewee_id,
            context=context,
            rating=rating,
            comment=text,
            revealed_at=now,
        )
        session.add(review)
    await session.flush()
    if existing is not None:
        await session.refresh(review, ["updated_at"])
    return review


@dataclass(frozen=True)
class UserReviewPage:
    items: list[models.Review]
    users: dict[UUID, models.User]
    next_cursor: str | None
    reputation: list[ReviewRatingSummary]


async def user_reviews(
    session: AsyncSession,
    *,
    target_id: UUID,
    context: ReviewContext | None,
    params: CursorParams,
) -> UserReviewPage:
    target = await session.get(models.User, target_id)
    if target is None or target.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.NOT_FOUND)
    statement = select(models.Review).where(
        models.Review.reviewee_id == target_id,
        models.Review.revealed_at.is_not(None),
        models.Review.removed_at.is_(None),
    )
    if context == ReviewContext.HOST:
        statement = statement.where(
            models.Review.context.in_([ReviewContext.HOST.value, ReviewContext.SPEAKER.value])
        )
    elif context is not None:
        statement = statement.where(models.Review.context == context)
    statement = apply_keyset(
        statement,
        params,
        sort=[models.Review.created_at],
        id_column=models.Review.id,
        descending=True,
    )
    rows = list(await session.scalars(statement))
    items = rows[: params.limit]
    next_cursor = (
        encode_cursor([items[-1].created_at, items[-1].id]) if len(rows) > params.limit else None
    )
    users = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_({review.reviewer_id for review in items}))
        )
    }
    summaries = await reputation_summary(session, target_id)
    return UserReviewPage(
        items=items,
        users=users,
        next_cursor=next_cursor,
        reputation=[
            ReviewRatingSummary(context=key, average=summary.avg, count=summary.count)
            for key, summary in summaries.items()
            if summary.count > 0
        ],
    )


async def load_reviews_received(
    session: AsyncSession, user_ids: Sequence[UUID]
) -> dict[UUID, list[ReviewIn]]:
    if not user_ids:
        return {}
    flags = {
        subject_id: FlagStatus(status)
        for subject_id, status in await session.execute(
            select(models.Flag.subject_id, models.Flag.status).where(
                models.Flag.subject_type == FlagSubjectType.REVIEW,
                models.Flag.status == FlagStatus.TO_CHECK,
            )
        )
    }
    reviews = await session.scalars(
        select(models.Review)
        .where(
            models.Review.reviewee_id.in_(user_ids),
            models.Review.revealed_at.is_not(None),
            models.Review.removed_at.is_(None),
        )
        .order_by(models.Review.created_at)
    )
    received: dict[UUID, list[ReviewIn]] = {user_id: [] for user_id in user_ids}
    for review in reviews:
        received[review.reviewee_id].append(
            ReviewIn(
                id=review.id,
                reviewer_id=review.reviewer_id,
                reviewee_id=review.reviewee_id,
                context=ReviewContext(review.context),
                rating=review.rating,
                subject_type=ReviewSubjectType(review.subject_type),
                subject_id=review.subject_id,
                comment=review.comment,
                created_at=review.created_at,
                revealed_at=review.revealed_at,
                removed_at=review.removed_at,
                flag_status=flags.get(review.id),
            )
        )
    return received
