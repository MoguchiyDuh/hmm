from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

import procrastinate
from hmm_contracts.common import EnrollmentStatus, FormationPhase, TeamStatus
from hmm_contracts.formation import placement_bounds
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.platform.settings import RuntimeSettings, load_settings


@dataclass(frozen=True, slots=True)
class FormationTiming:
    reg_deadline: datetime
    team_deadline: datetime
    formation_at: datetime
    review_ends_at: datetime


def formation_timing(
    event: models.Event, settings: RuntimeSettings, now: datetime
) -> FormationTiming:
    reg_deadline = event.registration_deadline or event.starts_at
    team_deadline = reg_deadline - timedelta(hours=settings.registration_buffer_hours)
    formation_at = team_deadline - timedelta(hours=settings.formation_lead_hours)
    min_notice = timedelta(hours=settings.formation_min_notice_hours)
    review_min = timedelta(hours=settings.review_min_hours)
    if formation_at < now + min_notice:
        formation_at = min(now + min_notice, team_deadline - review_min)
        if formation_at < now:
            team_deadline = reg_deadline - timedelta(hours=1)
            formation_at = min(now + min_notice, team_deadline - review_min)
        if formation_at < now:
            formation_at = now + timedelta(hours=1)
    return FormationTiming(
        reg_deadline=reg_deadline,
        team_deadline=team_deadline,
        formation_at=formation_at,
        review_ends_at=min(formation_at + timedelta(hours=settings.review_hours), team_deadline),
    )


async def ensure_formation(
    session: AsyncSession, event: models.Event, now: datetime
) -> models.EventFormation | None:
    if not event.formation_enabled:
        return None
    existing = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == event.id)
        .with_for_update()
    )
    if existing is not None:
        return existing
    settings = await load_settings(session)
    timing = formation_timing(event, settings, now)
    formation = models.EventFormation(
        event_id=event.id,
        phase=FormationPhase.OPEN.value,
        formation_at=timing.formation_at,
        review_ends_at=timing.review_ends_at,
    )
    session.add(formation)
    await session.flush()
    return formation


@dataclass(frozen=True, slots=True)
class EventDates:
    starts_at: datetime
    ends_at: datetime
    registration_deadline: datetime | None


def event_dates(event: models.Event) -> EventDates:
    return EventDates(
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        registration_deadline=event.registration_deadline,
    )


async def dates_changed(
    session: AsyncSession,
    jobs: procrastinate.App,
    event: models.Event,
    previous: EventDates,
    now: datetime,
) -> None:
    return None


def lead_eligibility(
    fit: models.LeadFit | None, settings: RuntimeSettings
) -> schemas.LeadEligibility | None:
    if fit is None:
        return None
    return schemas.LeadEligibility(
        fit=float(fit.fit),
        eligible=float(fit.fit) >= settings.lead_fit_threshold,
        reason=fit.reason,
        computed_at=fit.computed_at,
    )


async def formation_participation(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.Formation | None:
    if not event.formation_enabled:
        return None
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        return None
    settings = await load_settings(session)
    min_size = event.team_size_min or settings.team_size_default_min
    max_size = event.team_size_max or settings.team_size_default_max
    if min_size > max_size:
        max_size = min_size
    if max_size < 2:
        max_size = 2
    target = formation.target_override or max_size
    target = min(max(target, min_size), max_size)
    viable = min(max_size, max(min_size, ceil(target * settings.viable_ratio)))
    core_max = min(max_size, max(2, ceil(target * settings.core_ratio)))
    timing = formation_timing(event, settings, now)
    counts = {
        status: 0
        for status in (EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE)
    }
    volunteers = 0
    for status, count, willing in await session.execute(
        select(
            models.Enrollment.status,
            func.count(),
            func.count().filter(models.Enrollment.willing_to_lead),
        )
        .where(
            models.Enrollment.event_id == event.id,
            models.Enrollment.status.in_(
                [EnrollmentStatus.ACTIVE, EnrollmentStatus.PLACED, EnrollmentStatus.RESERVE]
            ),
        )
        .group_by(models.Enrollment.status)
    ):
        counts[EnrollmentStatus(status)] = count
        volunteers += willing
    cores = (
        await session.execute(
            select(models.Project.id, func.count(models.ProjectMember.id))
            .outerjoin(
                models.ProjectMember,
                (models.ProjectMember.project_id == models.Project.id)
                & models.ProjectMember.left_at.is_(None),
            )
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status == TeamStatus.FORMING,
            )
            .group_by(models.Project.id)
            .order_by(models.Project.created_at, models.Project.id)
        )
    ).all()
    teams = (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.event_id == event.id,
                models.Project.team_status.is_not(None),
                models.Project.team_status.not_in([TeamStatus.DISBANDED, TeamStatus.CANCELLED]),
            )
        )
        or 0
    )
    free = (
        await session.scalar(
            select(func.count())
            .select_from(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status == EnrollmentStatus.ACTIVE,
                ~exists().where(
                    models.ProjectMember.event_id == event.id,
                    models.ProjectMember.user_id == models.Enrollment.user_id,
                    models.ProjectMember.left_at.is_(None),
                ),
            )
        )
        or 0
    )
    cores_sizes = [min(max_size, max(1, count)) for _, count in cores]
    try:
        bounds = placement_bounds(free, cores_sizes, viable, target, max_size)
    except ValueError:
        return None
    remainder = bounds.placed - sum(cores_sizes)
    if bounds.new_teams == 0 or remainder <= 0:
        team_sizes = cores_sizes
    else:
        team_sizes = cores_sizes + [
            min(
                max_size,
                remainder // bounds.new_teams + (1 if index < remainder % bounds.new_teams else 0),
            )
            for index in range(bounds.new_teams)
        ]
    projection = schemas.FormationProjection(
        teams=bounds.teams,
        new_teams=bounds.new_teams,
        placed=bounds.placed,
        reserve=bounds.reserve,
        undersized_cores=bounds.undersized_cores,
        team_sizes=team_sizes,
        volunteers=volunteers,
    )
    fit = await session.get(models.LeadFit, (event.id, actor.id))
    return schemas.Formation(
        event_id=event.id,
        phase=FormationPhase(formation.phase),
        target=target,
        viable=viable,
        min_size=min_size,
        max_size=max_size,
        core_max=core_max,
        target_override=formation.target_override,
        formation_at_override=formation.formation_at_override,
        formation_at=formation.formation_at,
        review_ends_at=formation.review_ends_at,
        team_deadline=timing.team_deadline,
        reg_deadline=timing.reg_deadline,
        rounds=formation.rounds,
        last_run_id=formation.last_run_id,
        counts=schemas.FormationCounts(
            enrollments=sum(counts.values()),
            active=counts[EnrollmentStatus.ACTIVE],
            placed=counts[EnrollmentStatus.PLACED],
            reserve=counts[EnrollmentStatus.RESERVE],
            cores=len(cores),
            core_members=sum(count for _, count in cores),
            teams=teams,
            volunteers=volunteers,
        ),
        projection=projection,
        my_state=schemas.ParticipationState(
            enrollment=None, team=None, membership=None, lead_fit=lead_eligibility(fit, settings)
        ),
        allowed_actions=[],
    )
