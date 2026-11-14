from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipSide,
    MentorshipStatus,
    ReviewWindowSubjectType,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    LinkAction,
    MentorshipClosedPayload,
    MentorshipCloseProposedPayload,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed import service as feed
from hmm_core.mentorship import schemas, views
from hmm_core.notifications.service import notify
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.results.windows import mentorship_review_participants, open_review_window

CLOSE_PROPOSAL_WINDOW = timedelta(days=7)

CLOSABLE_STATUSES = (
    MentorshipStatus.ACTIVE.value,
    MentorshipStatus.STALLED.value,
    MentorshipStatus.CLOSING.value,
)


async def list_my_mentorships(
    session: AsyncSession,
    actor: models.User,
    role: MentorshipSide | None,
    mentorship_status: MentorshipStatus | None,
    now: datetime,
) -> list[schemas.Mentorship]:
    statement = select(models.Mentorship).where(
        or_(models.Mentorship.mentor_id == actor.id, models.Mentorship.learner_id == actor.id)
    )
    if role == MentorshipSide.MENTOR:
        statement = statement.where(models.Mentorship.mentor_id == actor.id)
    elif role == MentorshipSide.LEARNER:
        statement = statement.where(models.Mentorship.learner_id == actor.id)
    if mentorship_status is not None:
        statement = statement.where(models.Mentorship.status == mentorship_status.value)
    rows = list(
        await session.scalars(
            statement.order_by(models.Mentorship.started_at.desc(), models.Mentorship.id.desc())
        )
    )
    return [await views.mentorship_view(session, row, actor, now) for row in rows]


async def get_mentorship(
    session: AsyncSession, actor: models.User, mentorship_id: UUID, now: datetime
) -> schemas.Mentorship:
    mentorship = await session.get(models.Mentorship, mentorship_id)
    if mentorship is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id not in (mentorship.mentor_id, mentorship.learner_id) and not actor.is_admin:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return await views.mentorship_view(session, mentorship, actor, now)


async def create_checkin(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    mentorship_id: UUID,
    body: schemas.CreateMentorshipCheckinBody,
    now: datetime,
) -> schemas.MentorshipCheckin:
    mentorship = await session.scalar(
        select(models.Mentorship).where(models.Mentorship.id == mentorship_id).with_for_update()
    )
    if mentorship is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if actor.id not in (mentorship.mentor_id, mentorship.learner_id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    if mentorship.status not in (MentorshipStatus.ACTIVE.value, MentorshipStatus.STALLED.value):
        raise ProblemError(ErrorCode.INVALID_STATE)
    if body.occurred_on > now.date():
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Check-in date is in the future")
    if body.occurred_on < mentorship.started_at.date():
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR, "Check-in date is before the mentorship started"
        )
    existing = await session.scalar(
        select(models.MentorshipCheckin.id).where(
            models.MentorshipCheckin.mentorship_id == mentorship.id,
            models.MentorshipCheckin.author_id == actor.id,
            models.MentorshipCheckin.occurred_on == body.occurred_on,
        )
    )
    if existing is not None:
        raise ProblemError(ErrorCode.CHECKIN_LIMIT)
    row = models.MentorshipCheckin(
        mentorship_id=mentorship.id,
        author_id=actor.id,
        note=body.note,
        occurred_on=body.occurred_on,
        minutes=body.minutes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    mentorship.last_checkin_at = now
    if mentorship.status == MentorshipStatus.STALLED.value:
        mentorship.status = MentorshipStatus.ACTIVE.value
        mentorship.stalled_at = None
    await session.flush()
    for user_id in (mentorship.mentor_id, mentorship.learner_id):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return schemas.MentorshipCheckin(
        id=row.id,
        mentorship_id=row.mentorship_id,
        author_id=row.author_id,
        note=row.note,
        occurred_on=row.occurred_on,
        minutes=row.minutes,
        created_at=row.created_at,
    )


async def lock_mentorship(
    session: AsyncSession, actor: models.User, mentorship_id: UUID
) -> models.Mentorship:
    mentorship = await session.scalar(
        select(models.Mentorship).where(models.Mentorship.id == mentorship_id).with_for_update()
    )
    if mentorship is None or actor.id not in (mentorship.mentor_id, mentorship.learner_id):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return mentorship


async def mentorship_parties(
    session: AsyncSession, mentorship: models.Mentorship
) -> tuple[models.HelpRequest, models.Area, models.User, models.User]:
    request = await session.get(models.HelpRequest, mentorship.request_id)
    area = await session.get(models.Area, mentorship.area_id)
    mentor = await session.get(models.User, mentorship.mentor_id)
    learner = await session.get(models.User, mentorship.learner_id)
    if request is None or area is None or mentor is None or learner is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return request, area, mentor, learner


async def recompute_participants(
    session: AsyncSession, jobs: procrastinate.App, mentorship: models.Mentorship
) -> None:
    for user_id in (mentorship.mentor_id, mentorship.learner_id):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))


async def finalize_close(
    session: AsyncSession,
    jobs: procrastinate.App,
    mentorship: models.Mentorship,
    *,
    now: datetime,
    outcome: MentorshipOutcome | None,
    summary: str | None,
) -> None:
    if mentorship.status not in CLOSABLE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    request, area, mentor, learner = await mentorship_parties(session, mentorship)
    participants = await mentorship_review_participants(session, mentorship_id=mentorship.id)
    mentorship.status = MentorshipStatus.CLOSED.value
    mentorship.outcome = outcome.value if outcome is not None else None
    mentorship.summary = summary
    mentorship.closed_at = now
    mentorship.stalled_at = None
    await session.flush()
    window = await open_review_window(
        session,
        subject_type=ReviewWindowSubjectType.MENTORSHIP,
        subject_id=mentorship.id,
        participants=participants,
        now=now,
    )
    if window is None:
        raise ProblemError(ErrorCode.INTERNAL_ERROR, "Close must open a review window")
    await feed.record_mentorship_completed(session, mentorship.id, now)
    link = f"/mentorships/{mentorship.id}"
    for user_id in (mentorship.mentor_id, mentorship.learner_id):
        actions: tuple[ApiAction | LinkAction, ...] = ()
        if user_id == mentorship.mentor_id and outcome is not None:
            actions = (
                ApiAction(
                    kind="api",
                    key=ActionKey.MENTORSHIP_ACKNOWLEDGE,
                    entity_id=mentorship.id,
                    label_key="notifications.actions.mentorship_acknowledge",
                ),
                LinkAction(kind="link", label_key="notifications.actions.endorse", url=link),
            )
        await notify(
            session,
            user_id,
            MentorshipClosedPayload(
                link=link,
                actions=actions,
                mentorship_id=mentorship.id,
                help_request_id=request.id,
                mentor=views.notification_person(mentor),
                learner=views.notification_person(learner),
                area_key=area.key,
                area_name=area.name,
                topic=request.topic,
                goal=request.goal,
                kind=MentorshipKind(mentorship.kind),
                outcome=outcome,
                summary=summary,
                closed_at=now,
                review_window_id=window.id,
                review_closes_at=window.closes_at,
            ),
            now,
            dedupe_key=f"mentorship_closed:{mentorship.id}:{user_id}",
        )
    await recompute_participants(session, jobs, mentorship)


async def close_mentorship(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    mentorship_id: UUID,
    body: schemas.CloseMentorshipBody,
    now: datetime,
) -> schemas.Mentorship:
    mentorship = await lock_mentorship(session, actor, mentorship_id)
    if actor.id != mentorship.learner_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if mentorship.status not in CLOSABLE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    await feed.add_confirmation(
        session,
        jobs,
        fact_type=ConfirmationFactType.MENTORSHIP_SIDE,
        fact_id=mentorship.id,
        subject_user_id=mentorship.mentor_id,
        confirmer_id=mentorship.learner_id,
        source=ConfirmationSource.LEARNER
        if mentorship.kind == MentorshipKind.MENTORING.value
        else ConfirmationSource.PEER,
        now=now,
    )
    await finalize_close(
        session, jobs, mentorship, now=now, outcome=body.outcome, summary=body.summary
    )
    await session.flush()
    await session.refresh(mentorship)
    return await views.mentorship_view(session, mentorship, actor, now)


async def propose_close(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    mentorship_id: UUID,
    now: datetime,
) -> schemas.Mentorship:
    mentorship = await lock_mentorship(session, actor, mentorship_id)
    if actor.id != mentorship.mentor_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if mentorship.status not in (MentorshipStatus.ACTIVE.value, MentorshipStatus.STALLED.value):
        raise ProblemError(ErrorCode.INVALID_STATE)
    mentorship.status = MentorshipStatus.CLOSING.value
    mentorship.close_proposed_at = now
    await session.flush()
    request, area, mentor, learner = await mentorship_parties(session, mentorship)
    link = f"/mentorships/{mentorship.id}"
    await notify(
        session,
        mentorship.learner_id,
        MentorshipCloseProposedPayload(
            link=link,
            actions=(
                LinkAction(
                    kind="link", label_key="notifications.actions.close_with_outcome", url=link
                ),
            ),
            mentorship_id=mentorship.id,
            help_request_id=request.id,
            mentor=views.notification_person(mentor),
            learner=views.notification_person(learner),
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            kind=MentorshipKind(mentorship.kind),
            respond_by=now + CLOSE_PROPOSAL_WINDOW,
        ),
        now,
        dedupe_key=f"mentorship_close_proposed:{mentorship.id}:{now.isoformat()}",
    )
    await recompute_participants(session, jobs, mentorship)
    await session.flush()
    await session.refresh(mentorship)
    return await views.mentorship_view(session, mentorship, actor, now)


async def acknowledge_outcome(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    mentorship_id: UUID,
    body: schemas.AcknowledgeMentorshipBody,
    now: datetime,
) -> schemas.Mentorship:
    mentorship = await lock_mentorship(session, actor, mentorship_id)
    if actor.id != mentorship.mentor_id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if (
        mentorship.status != MentorshipStatus.CLOSED.value
        or mentorship.outcome is None
        or mentorship.mentor_acknowledged_at is not None
    ):
        raise ProblemError(ErrorCode.INVALID_STATE)
    mentorship.mentor_acknowledged_at = now
    mentorship.endorsement = body.endorsement
    await feed.add_confirmation(
        session,
        jobs,
        fact_type=ConfirmationFactType.MENTORSHIP_SIDE,
        fact_id=mentorship.id,
        subject_user_id=mentorship.learner_id,
        confirmer_id=mentorship.mentor_id,
        source=ConfirmationSource.MENTOR
        if mentorship.kind == MentorshipKind.MENTORING.value
        else ConfirmationSource.PEER,
        now=now,
    )
    await recompute_participants(session, jobs, mentorship)
    await session.flush()
    await session.refresh(mentorship)
    return await views.mentorship_view(session, mentorship, actor, now)


async def cancel_mentorship(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    mentorship_id: UUID,
    now: datetime,
) -> schemas.Mentorship:
    mentorship = await lock_mentorship(session, actor, mentorship_id)
    if mentorship.last_checkin_at is not None or mentorship.status not in CLOSABLE_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE)
    mentorship.status = MentorshipStatus.CANCELLED.value
    mentorship.closed_at = None
    mentorship.outcome = None
    mentorship.stalled_at = None
    await session.flush()
    await recompute_participants(session, jobs, mentorship)
    await session.refresh(mentorship)
    return await views.mentorship_view(session, mentorship, actor, now)
