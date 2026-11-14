from datetime import timedelta

from hmm_contracts.common import EventStatus
from sqlalchemy import update

from hmm_core import models
from hmm_core.platform.jobs import JobName, Runtime, job
from hmm_core.platform.settings import load_settings


@job(JobName.ARCHIVE_OLD_EVENTS)
async def archive_old_events(runtime: Runtime) -> None:
    now = runtime.clock.now()
    async with runtime.sessionmaker.begin() as session:
        settings = await load_settings(session)
        cutoff = now - timedelta(days=settings.event_archive_after_days)
        await session.execute(
            update(models.Event)
            .where(
                models.Event.status.in_(
                    [
                        EventStatus.DRAFT.value,
                        EventStatus.PUBLISHED.value,
                        EventStatus.CANCELLED.value,
                    ]
                ),
                models.Event.ends_at < cutoff,
            )
            .values(status=EventStatus.ARCHIVED.value)
        )
