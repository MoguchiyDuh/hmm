from uuid import UUID

from hmm_contracts.common import EventStatus, FormationPhase
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service
from hmm_core.identity.service import ONBOARDED
from hmm_core.platform.algo import call_algo
from hmm_core.platform.jobs import JobName, Runtime, defer_job, job
from hmm_core.scores.snapshot import build_member_snapshots


async def _refresh_event(
    runtime: Runtime,
    session: AsyncSession,
    event: models.Event,
    user_ids: set[UUID] | None = None,
) -> None:
    now = runtime.clock.now()
    formation = await session.get(models.EventFormation, event.id)
    if formation is None or FormationPhase(formation.phase) not in service.LEAD_FIT_PHASES:
        return None
    candidates = await service.lead_fit_candidates(session, event.id, now)
    ids = [user.id for user in candidates if user_ids is None or user.id in user_ids]
    if not ids:
        return None
    snapshots = await build_member_snapshots(session, ids, now)
    members = [snapshots[user_id] for user_id in ids if user_id in snapshots]
    if not members:
        return None
    snapshot = await catalog.event_snapshot(session, event.id)
    ctx = await runtime.algo_context(session)
    fits = await call_algo("lead_fit", runtime.algorithm.lead_fit(ctx, snapshot, members))
    await service.store_lead_fits(
        session,
        event.id,
        service.validated_lead_fits({member.user_id for member in members}, fits),
        runtime.algorithm.version,
        now,
    )
    return None


@job(JobName.COMPUTE_LEAD_FITS)
async def compute_lead_fits(runtime: Runtime, event_id: str | None = None) -> None:
    async with runtime.sessionmaker.begin() as session:
        if event_id is None:
            for event in await service.lead_fit_events(session):
                await defer_job(
                    session, runtime.jobs, JobName.COMPUTE_LEAD_FITS, event_id=str(event.id)
                )
            return None
        event = await catalog.load_event(session, UUID(event_id))
        if event.status != EventStatus.PUBLISHED.value or not event.formation_enabled:
            return None
        await _refresh_event(runtime, session, event)
    return None


@job(JobName.COMPUTE_LEAD_FITS_FOR_MEMBER)
async def compute_lead_fits_for_member(runtime: Runtime, user_id: str) -> None:
    async with runtime.sessionmaker.begin() as session:
        user = await session.get(models.User, UUID(user_id))
        if (
            user is None
            or user.onboarding_status not in ONBOARDED
            or user.banned_at is not None
            or service.dnd_active(user, runtime.clock.now())
        ):
            return None
        for event in await service.lead_fit_events_for_member(session, user.id):
            await _refresh_event(runtime, session, event, {user.id})
    return None
