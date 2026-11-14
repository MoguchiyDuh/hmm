from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hmm_contracts.common import ErrorCode, MentorshipKind, ReviewContext, ReviewSubjectType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.schemas import UserRef
from hmm_core.results.schemas import Review, ReviewAction
from hmm_core.results.windows import subject_details, window_participants


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
