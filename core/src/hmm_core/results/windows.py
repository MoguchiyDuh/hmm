from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    MemberRole,
    MemberState,
    MentorshipKind,
    ReviewContext,
    ReviewSubjectType,
    ReviewWindowSubjectType,
)
from hmm_contracts.notifications import (
    LinkAction,
    NotificationPerson,
    ReviewReceivedPayload,
    ReviewsOpenPayload,
    ReviewsPendingPayload,
)
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings

REVIEW_ACTION_LABEL = "notifications.actions.review"
REMINDER_LEAD = timedelta(days=2)


async def subject_details(
    session: AsyncSession, *, subject_type: ReviewSubjectType, subject_id: UUID
) -> tuple[str, str]:
    if subject_type == ReviewSubjectType.PROJECT:
        project = await session.get(models.Project, subject_id)
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        return project.name, f"/projects/{subject_id}"
    if subject_type == ReviewSubjectType.MENTORSHIP:
        mentorship = await session.get(models.Mentorship, subject_id)
        if mentorship is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        topic = (
            await session.execute(
                select(models.HelpRequest.topic).where(
                    models.HelpRequest.id == mentorship.request_id
                )
            )
        ).scalar_one()
        return topic, f"/mentorships/{subject_id}"
    event = await session.get(models.Event, subject_id)
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event.title, f"/events/{subject_id}"


def window_participants(window: models.ReviewWindow) -> dict[UUID, ReviewContext]:
    return {
        UUID(key): ReviewContext(value)
        for key, value in cast(dict[str, str], window.participants).items()
    }


async def result_review_participants(
    session: AsyncSession, *, project_id: UUID
) -> dict[UUID, ReviewContext]:
    result = await session.get(models.ProjectResult, project_id)
    if result is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Project has no result")
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return {
        UUID(user_id): ReviewContext.LEAD
        if UUID(user_id) == project.lead_user_id
        else ReviewContext.MEMBER
        for user_id, participated in cast(dict[str, bool], result.participants).items()
        if participated is True
    }


async def release_review_participants(
    session: AsyncSession, *, project_id: UUID, now: datetime
) -> dict[UUID, ReviewContext]:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    min_days = (await load_settings(session)).project_review_min_days
    cutoff = now - timedelta(days=min_days)
    members = await session.execute(
        select(models.ProjectMember.user_id, models.ProjectMember.role).where(
            models.ProjectMember.project_id == project_id,
            models.ProjectMember.state == MemberState.CONFIRMED,
            models.ProjectMember.joined_at <= cutoff,
            or_(models.ProjectMember.left_at.is_(None), models.ProjectMember.left_at > now),
        )
    )
    return {
        user_id: ReviewContext.LEAD
        if role == MemberRole.LEAD or user_id == project.lead_user_id
        else ReviewContext.MEMBER
        for user_id, role in members
    }


async def mentorship_review_participants(
    session: AsyncSession, *, mentorship_id: UUID
) -> dict[UUID, ReviewContext]:
    mentorship = await session.get(models.Mentorship, mentorship_id)
    if mentorship is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    context = (
        ReviewContext.MENTOR
        if mentorship.kind == MentorshipKind.MENTORING
        else ReviewContext.MEMBER
    )
    return {mentorship.mentor_id: context, mentorship.learner_id: context}


async def open_review_window(
    session: AsyncSession,
    *,
    subject_type: ReviewWindowSubjectType,
    subject_id: UUID,
    participants: Mapping[UUID, ReviewContext],
    now: datetime,
) -> models.ReviewWindow | None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"review_window:{subject_type.value}:{subject_id}"},
    )
    existing = await session.scalar(
        select(models.ReviewWindow)
        .where(
            models.ReviewWindow.subject_type == subject_type,
            models.ReviewWindow.subject_id == subject_id,
            models.ReviewWindow.revealed_at.is_(None),
            models.ReviewWindow.closes_at > now,
        )
        .with_for_update()
    )
    if existing is not None:
        return existing
    if len(participants) < 2:
        return None
    closes_at = now + timedelta(days=(await load_settings(session)).review_window_days)
    window = models.ReviewWindow(
        subject_type=subject_type,
        subject_id=subject_id,
        opens_at=now,
        closes_at=closes_at,
        participants={str(user_id): context.value for user_id, context in participants.items()},
    )
    session.add(window)
    await session.flush()
    title, link = await subject_details(
        session, subject_type=ReviewSubjectType(subject_type.value), subject_id=subject_id
    )
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.REVIEW,
        entity_id=window.id,
        after={
            "subject_type": subject_type.value,
            "subject_id": str(subject_id),
            "opens_at": now.isoformat(),
            "closes_at": closes_at.isoformat(),
        },
    )
    users = await session.scalars(select(models.User).where(models.User.id.in_(list(participants))))
    people = tuple(
        NotificationPerson(user_id=user.id, display_name=user.display_name)
        for user in sorted(users, key=lambda user: (user.display_name, str(user.id)))
    )
    for user_id in participants:
        await notify(
            session,
            user_id,
            ReviewsOpenPayload(
                link=link,
                actions=(LinkAction(kind="link", label_key=REVIEW_ACTION_LABEL, url=link),),
                review_window_id=window.id,
                subject_type=subject_type,
                subject_id=subject_id,
                subject_title=title,
                closes_at=closes_at,
                to_review=tuple(person for person in people if person.user_id != user_id),
                submitted_count=0,
            ),
            now,
            dedupe_key=f"reviews_open:{window.id}:{user_id}",
        )
    return window


async def send_review_reminders(session: AsyncSession, now: datetime) -> int:
    windows = await session.scalars(
        select(models.ReviewWindow)
        .where(
            models.ReviewWindow.revealed_at.is_(None),
            models.ReviewWindow.closes_at > now,
            models.ReviewWindow.closes_at <= now + REMINDER_LEAD,
        )
        .order_by(models.ReviewWindow.closes_at)
    )
    sent = 0
    for window in windows:
        participants = window_participants(window)
        if len(participants) < 2:
            continue
        pairs = await session.execute(
            select(models.Review.reviewer_id, models.Review.reviewee_id).where(
                models.Review.window_id == window.id
            )
        )
        submitted: dict[UUID, set[UUID]] = {}
        for reviewer_id, reviewee_id in pairs:
            submitted.setdefault(reviewer_id, set()).add(reviewee_id)
        users = await session.scalars(
            select(models.User).where(models.User.id.in_(list(participants)))
        )
        people = tuple(
            NotificationPerson(user_id=user.id, display_name=user.display_name)
            for user in sorted(users, key=lambda user: (user.display_name, str(user.id)))
        )
        title, link = await subject_details(
            session,
            subject_type=ReviewSubjectType(window.subject_type),
            subject_id=window.subject_id,
        )
        for user_id in participants:
            reviewed = submitted.get(user_id, set())
            outstanding = participants.keys() - {user_id} - reviewed
            if not outstanding:
                continue
            notification = await notify(
                session,
                user_id,
                ReviewsPendingPayload(
                    link=link,
                    actions=(LinkAction(kind="link", label_key=REVIEW_ACTION_LABEL, url=link),),
                    review_window_id=window.id,
                    subject_type=ReviewWindowSubjectType(window.subject_type),
                    subject_id=window.subject_id,
                    subject_title=title,
                    closes_at=window.closes_at,
                    to_review=tuple(person for person in people if person.user_id in outstanding),
                    submitted_count=len(reviewed),
                ),
                now,
                dedupe_key=f"reviews_pending:{window.id}:{user_id}",
            )
            if notification is not None:
                sent += 1
    return sent


async def reveal_due_windows(session: AsyncSession, now: datetime) -> int:
    windows = list(
        await session.scalars(
            select(models.ReviewWindow)
            .where(
                models.ReviewWindow.revealed_at.is_(None),
                models.ReviewWindow.closes_at <= now,
            )
            .with_for_update()
        )
    )
    for window in windows:
        reviews = await session.scalars(
            select(models.Review).where(models.Review.window_id == window.id).with_for_update()
        )
        counts: dict[tuple[UUID, ReviewContext], int] = {}
        for review in reviews:
            if review.revealed_at is None:
                review.revealed_at = now
            if review.removed_at is None:
                key = review.reviewee_id, ReviewContext(review.context)
                counts[key] = counts.get(key, 0) + 1
        window.revealed_at = now
        title, link = await subject_details(
            session,
            subject_type=ReviewSubjectType(window.subject_type),
            subject_id=window.subject_id,
        )
        for (reviewee_id, context), count in counts.items():
            await notify(
                session,
                reviewee_id,
                ReviewReceivedPayload(
                    link=link,
                    actions=(),
                    review_window_id=window.id,
                    subject_id=window.subject_id,
                    subject_title=title,
                    review_count=count,
                    context=context,
                    revealed_at=now,
                ),
                now,
                dedupe_key=f"review_received:{window.id}:{reviewee_id}:{context.value}",
            )
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.REVIEW,
            entity_id=window.id,
            after={"revealed_at": now.isoformat()},
        )
    return len(windows)


async def run_review_timers(session: AsyncSession, now: datetime) -> None:
    await reveal_due_windows(session, now)
    await send_review_reminders(session, now)
