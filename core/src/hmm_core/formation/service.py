from dataclasses import dataclass
from datetime import datetime, timedelta

import procrastinate
from hmm_contracts.common import FormationPhase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
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
