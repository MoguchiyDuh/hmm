from datetime import datetime, timedelta

from hmm_contracts.common import FlagStatus
from hmm_contracts.notifications import AdminDigestPayload
from sqlalchemy import func, select

from hmm_core import models
from hmm_core.admin.service import check_parser_down, dashboard
from hmm_core.notifications.service import notify_many
from hmm_core.platform.jobs import JobName, Runtime, job
from hmm_core.platform.settings import load_settings


def digest_period(now: datetime) -> tuple[datetime, datetime]:
    end = now.replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    if end > now:
        end -= timedelta(days=7)
    return end - timedelta(days=7), end


@job(JobName.ADMIN_DIGEST)
async def admin_digest(runtime: Runtime) -> None:
    async with runtime.sessionmaker.begin() as session:
        now = runtime.clock.now()
        start, end = digest_period(now)
        settings = await load_settings(session)
        await check_parser_down(session, now, settings)
        counters = await dashboard(session, now)
        overdue_to_check_items = await session.scalar(
            select(func.count(models.Flag.id)).where(
                models.Flag.status == FlagStatus.TO_CHECK.value,
                models.Flag.created_at < now - timedelta(days=7),
            )
        )
        payload = AdminDigestPayload(
            link="/admin/dashboard",
            period_starts_at=start,
            period_ends_at=end,
            pending_member_reviews=counters.pending_member_reviews,
            overdue_to_check_items=overdue_to_check_items or 0,
            member_event_duplicates=counters.member_event_duplicates,
            results_to_verify=counters.results_to_verify,
            open_alerts=counters.open_alerts,
        )
        admin_ids = list(
            await session.scalars(
                select(models.User.id).where(
                    models.User.is_admin.is_(True), models.User.banned_at.is_(None)
                )
            )
        )
        await notify_many(
            session, admin_ids, payload, now, dedupe_key=f"admin_digest:{end.isoformat()}"
        )
