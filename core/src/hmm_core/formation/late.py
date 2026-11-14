from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    EnrollmentStatus,
    EventStatus,
    FormationKind,
    FormationPhase,
    ProjectOrigin,
    TeamStatus,
)
from hmm_contracts.notifications import LeadOpportunityPayload
from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    ONBOARDED_STATUSES,
    FormationSizes,
    FormationTiming,
    dnd_active,
    formation_sizes,
    formation_timing,
)
from hmm_core.notifications.service import notify_many
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import RuntimeSettings, load_settings


def _late_claim_candidates_query(event_id: UUID, settings: RuntimeSettings) -> Select[models.User]:
    return (
        select(models.User)
        .join(models.Enrollment, models.Enrollment.user_id == models.User.id)
        .outerjoin(
            models.LeadFit,
            and_(
                models.LeadFit.event_id == event_id,
                models.LeadFit.user_id == models.Enrollment.user_id,
            ),
        )
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.status == EnrollmentStatus.RESERVE.value,
            models.User.banned_at.is_(None),
            models.User.onboarding_status.in_(sorted(ONBOARDED_STATUSES)),
            models.LeadFit.fit.is_not(None),
            models.LeadFit.fit >= settings.lead_fit_threshold,
        )
    )


async def late_claim_eligible(session: AsyncSession, event: models.Event, user_id: UUID) -> bool:
    settings = await load_settings(session)
    return (
        await session.scalar(
            _late_claim_candidates_query(event.id, settings)
            .where(models.User.id == user_id)
            .limit(1)
        )
        is not None
    )


def late_core_window_end(
    project: models.Project, settings: RuntimeSettings, timing: FormationTiming
) -> datetime:
    return min(
        project.created_at + timedelta(hours=settings.late_core_hours),
        timing.reg_deadline - timedelta(hours=settings.review_min_hours),
    )


async def _late_round_ready(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    settings: RuntimeSettings,
    sizes: FormationSizes,
    timing: FormationTiming,
    now: datetime,
) -> bool:
    if now >= timing.reg_deadline - timedelta(hours=settings.review_min_hours):
        return False
    projects = (
        await session.execute(
            select(models.Project.id, models.Project.seats_closed_at).where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.LOCKED.value,
            )
        )
    ).all()
    counts = {
        project_id: count
        for project_id, count in await session.execute(
            select(models.ProjectMember.project_id, func.count())
            .where(
                models.ProjectMember.event_id == event.id,
                models.ProjectMember.left_at.is_(None),
            )
            .group_by(models.ProjectMember.project_id)
        )
    }
    if any(
        counts.get(project_id, 0) < sizes.target and seats_closed_at is None
        for project_id, seats_closed_at in projects
    ):
        return False
    reserve_count = await _reserve_count(session, event.id)
    return reserve_count >= sizes.viable


async def _reserve_count(session: AsyncSession, event_id: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .join(models.User, models.User.id == models.Enrollment.user_id)
            .where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status == EnrollmentStatus.RESERVE.value,
                models.User.banned_at.is_(None),
            )
        )
        or 0
    )


async def run_late_round(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    formation: models.EventFormation,
    now: datetime,
) -> bool:
    if event.status != EventStatus.PUBLISHED.value:
        return False
    if FormationPhase(formation.phase) != FormationPhase.LOCKED:
        return False
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    timing = formation_timing(event, settings, now)
    cores = list(
        await session.scalars(
            select(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.origin == ProjectOrigin.LATE_CLAIM.value,
                models.Project.team_status == TeamStatus.FORMING.value,
            )
            .order_by(models.Project.created_at, models.Project.id)
        )
    )
    if cores:
        counts = {
            project_id: count
            for project_id, count in await session.execute(
                select(models.ProjectMember.project_id, func.count())
                .where(
                    models.ProjectMember.project_id.in_([core.id for core in cores]),
                    models.ProjectMember.left_at.is_(None),
                )
                .group_by(models.ProjectMember.project_id)
            )
        }
        due = all(
            counts.get(core.id, 0) >= sizes.core_max
            or now >= late_core_window_end(core, settings, timing)
            for core in cores
        )
        if due:
            await defer_job(
                session,
                jobs,
                JobName.RUN_FORMATION,
                event_id=str(event.id),
                kind=FormationKind.LATE.value,
            )
            formation.late_round_at = None
            await session.flush()
            return True
        return False
    if not await _late_round_ready(session, event, formation, settings, sizes, timing, now):
        if formation.late_round_at is not None:
            formation.late_round_at = None
            await session.flush()
        return False
    late_round_at = formation.late_round_at
    if late_round_at is None:
        late_round_at = now
        formation.late_round_at = now
        await session.flush()
    eligible = [
        user
        for user in await session.scalars(_late_claim_candidates_query(event.id, settings))
        if not dnd_active(user, now)
    ]
    if eligible:
        payload = LeadOpportunityPayload(
            link=f"/events/{event.id}/formation",
            event_id=event.id,
            event_title=event.title,
            expires_at=min(
                late_round_at + timedelta(hours=settings.late_claim_grace_hours),
                timing.reg_deadline - timedelta(hours=settings.review_min_hours),
            ),
            reserve_count=await _reserve_count(session, event.id),
            viable_size=sizes.viable,
        )
        await notify_many(
            session,
            [user.id for user in eligible],
            payload,
            now,
            dedupe_key=f"lead_opportunity:{event.id}:{now.date().isoformat()}",
        )
    if now >= late_round_at + timedelta(hours=settings.late_claim_grace_hours):
        await defer_job(
            session,
            jobs,
            JobName.RUN_FORMATION,
            event_id=str(event.id),
            kind=FormationKind.LATE.value,
        )
        formation.late_round_at = None
        await session.flush()
        return True
    await session.flush()
    return False
