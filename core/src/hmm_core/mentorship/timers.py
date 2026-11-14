from datetime import datetime, timedelta

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    MentorshipKind,
    MentorshipStatus,
)
from hmm_contracts.notifications import LinkAction, MentorshipIdlePayload, MentorshipStalledPayload
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.mentorship import views
from hmm_core.mentorship.service import CLOSE_PROPOSAL_WINDOW, finalize_close
from hmm_core.notifications.service import notify
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings

IDLE_WARNING = timedelta(days=4)


async def _facts(
    session: AsyncSession, mentorship: models.Mentorship
) -> tuple[models.HelpRequest, models.Area, models.User, models.User]:
    request = await session.get(models.HelpRequest, mentorship.request_id)
    area = await session.get(models.Area, mentorship.area_id)
    mentor = await session.get(models.User, mentorship.mentor_id)
    learner = await session.get(models.User, mentorship.learner_id)
    if request is None or area is None or mentor is None or learner is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return request, area, mentor, learner


def _check_in_action(mentorship: models.Mentorship) -> tuple[LinkAction, ...]:
    return (
        LinkAction(
            kind="link",
            label_key="notifications.actions.check_in",
            url=f"/mentorships/{mentorship.id}",
        ),
    )


async def send_idle_warnings(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 50
) -> int:
    settings = await load_settings(session)
    idle = timedelta(days=settings.mentorship_idle_days)
    inactive_since = func.coalesce(models.Mentorship.last_checkin_at, models.Mentorship.started_at)
    rows = list(
        await session.scalars(
            select(models.Mentorship)
            .where(
                models.Mentorship.status == MentorshipStatus.ACTIVE.value,
                inactive_since <= now - (idle - IDLE_WARNING),
            )
            .order_by(inactive_since, models.Mentorship.id)
            .limit(limit)
            .with_for_update()
        )
    )
    warned = 0
    for mentorship in rows:
        request, area, mentor, learner = await _facts(session, mentorship)
        reference = mentorship.last_checkin_at or mentorship.started_at
        payload = MentorshipIdlePayload(
            link=f"/mentorships/{mentorship.id}",
            actions=_check_in_action(mentorship),
            mentorship_id=mentorship.id,
            help_request_id=request.id,
            mentor=views.notification_person(mentor),
            learner=views.notification_person(learner),
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            kind=MentorshipKind(mentorship.kind),
            idle_days=(now - reference).days,
            stalls_at=reference + idle,
        )
        created = False
        for user_id in (mentorship.mentor_id, mentorship.learner_id):
            row = await notify(
                session,
                user_id,
                payload,
                now,
                dedupe_key=f"mentorship_idle:{mentorship.id}:{user_id}:{reference.isoformat()}",
            )
            created = created or row is not None
        if created:
            warned += 1
    return warned


async def stall_mentorships(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 50
) -> int:
    settings = await load_settings(session)
    idle = timedelta(days=settings.mentorship_idle_days)
    inactive_since = func.coalesce(models.Mentorship.last_checkin_at, models.Mentorship.started_at)
    rows = list(
        await session.scalars(
            select(models.Mentorship)
            .where(
                models.Mentorship.status == MentorshipStatus.ACTIVE.value,
                inactive_since <= now - idle,
            )
            .order_by(inactive_since, models.Mentorship.id)
            .limit(limit)
            .with_for_update()
        )
    )
    for mentorship in rows:
        reference = mentorship.last_checkin_at or mentorship.started_at
        mentorship.status = MentorshipStatus.STALLED.value
        mentorship.stalled_at = now
        await session.flush()
        request, area, mentor, learner = await _facts(session, mentorship)
        payload = MentorshipStalledPayload(
            link=f"/mentorships/{mentorship.id}",
            actions=_check_in_action(mentorship),
            mentorship_id=mentorship.id,
            help_request_id=request.id,
            mentor=views.notification_person(mentor),
            learner=views.notification_person(learner),
            area_key=area.key,
            area_name=area.name,
            topic=request.topic,
            goal=request.goal,
            kind=MentorshipKind(mentorship.kind),
            idle_days=(now - reference).days,
            closes_at=now + idle,
        )
        for user_id in (mentorship.mentor_id, mentorship.learner_id):
            await notify(
                session,
                user_id,
                payload,
                now,
                dedupe_key=f"mentorship_stalled:{mentorship.id}:{user_id}:{now.isoformat()}",
            )
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.MENTORSHIP,
            entity_id=mentorship.id,
            before={"status": MentorshipStatus.ACTIVE.value},
            after={"status": MentorshipStatus.STALLED.value, "stalled_at": now.isoformat()},
        )
        for user_id in (mentorship.mentor_id, mentorship.learner_id):
            await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return len(rows)


async def close_stalled_mentorships(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 50
) -> int:
    settings = await load_settings(session)
    idle = timedelta(days=settings.mentorship_idle_days)
    rows = list(
        await session.scalars(
            select(models.Mentorship)
            .where(
                models.Mentorship.status == MentorshipStatus.STALLED.value,
                models.Mentorship.stalled_at <= now - idle,
            )
            .order_by(models.Mentorship.stalled_at, models.Mentorship.id)
            .limit(limit)
            .with_for_update()
        )
    )
    for mentorship in rows:
        await finalize_close(session, jobs, mentorship, now=now, outcome=None, summary=None)
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.MENTORSHIP,
            entity_id=mentorship.id,
            before={"status": MentorshipStatus.STALLED.value},
            after={
                "status": MentorshipStatus.CLOSED.value,
                "outcome": None,
                "closed_at": now.isoformat(),
            },
            reason="stalled_auto_closed",
        )
    return len(rows)


async def close_expired_proposals(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 50
) -> int:
    rows = list(
        await session.scalars(
            select(models.Mentorship)
            .where(
                models.Mentorship.status == MentorshipStatus.CLOSING.value,
                models.Mentorship.close_proposed_at <= now - CLOSE_PROPOSAL_WINDOW,
            )
            .order_by(models.Mentorship.close_proposed_at, models.Mentorship.id)
            .limit(limit)
            .with_for_update()
        )
    )
    for mentorship in rows:
        await finalize_close(session, jobs, mentorship, now=now, outcome=None, summary=None)
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.SYSTEM_TRANSITION,
            entity_type=AuditEntityType.MENTORSHIP,
            entity_id=mentorship.id,
            before={"status": MentorshipStatus.CLOSING.value},
            after={
                "status": MentorshipStatus.CLOSED.value,
                "outcome": None,
                "closed_at": now.isoformat(),
            },
            reason="close_proposal_expired",
        )
    return len(rows)


async def run_mentorship_timers(
    session: AsyncSession, jobs: procrastinate.App, now: datetime, *, limit: int = 50
) -> None:
    await send_idle_warnings(session, jobs, now, limit=limit)
    await stall_mentorships(session, jobs, now, limit=limit)
    await close_stalled_mentorships(session, jobs, now, limit=limit)
    await close_expired_proposals(session, jobs, now, limit=limit)
