from datetime import datetime, timedelta

from sqlalchemy import func, select

from hmm import models
from hmm.admin.service import dashboard
from hmm.common import FlagStatus, MemberReviewStatus
from hmm.notifications.payloads import AdminDigestPayload
from hmm.notifications.service import notify_many
from hmm.platform.jobs import JobName, Runtime, job
from hmm.platform.settings import load_settings


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
        counters = await dashboard(session, now)
        stale_member_reviews = await session.scalar(
            select(func.count(models.MemberReview.user_id)).where(
                models.MemberReview.status == MemberReviewStatus.PENDING.value,
                models.MemberReview.opened_at
                < now - timedelta(days=settings.member_review_reminder_days),
            )
        )
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
            pending_member_reviews=stale_member_reviews or 0,
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
