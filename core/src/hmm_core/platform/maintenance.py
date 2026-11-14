from datetime import timedelta

from hmm_contracts.common import DeliveryStatus
from sqlalchemy import delete, or_

from hmm_core import models
from hmm_core.platform.jobs import JobName, Runtime, job

DELIVERY_RETENTION = timedelta(days=30)


@job(JobName.CLEANUP)
async def cleanup(runtime: Runtime) -> None:
    now = runtime.clock.now()
    async with runtime.sessionmaker.begin() as session:
        await session.execute(
            delete(models.UserSession).where(models.UserSession.expires_at <= now)
        )
        await session.execute(
            delete(models.TelegramLinkCode).where(
                or_(
                    models.TelegramLinkCode.expires_at <= now,
                    models.TelegramLinkCode.used_at.is_not(None),
                )
            )
        )
        await session.execute(
            delete(models.NotificationDelivery).where(
                models.NotificationDelivery.status.in_(
                    [DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.SKIPPED]
                ),
                models.NotificationDelivery.updated_at < now - DELIVERY_RETENTION,
            )
        )
        await session.execute(delete(models.AlgoCache).where(models.AlgoCache.expires_at <= now))
