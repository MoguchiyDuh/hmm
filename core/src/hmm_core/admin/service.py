from datetime import datetime, timedelta

from hmm_contracts.common import (
    AlertStatus,
    EventOrigin,
    FlagStatus,
    FormationPhase,
    HelpOfferStatus,
    HelpRequestStatus,
    MemberReviewStatus,
    PlaceStatus,
)
from hmm_contracts.notifications import AlertKind
from sqlalchemy import Text, and_, exists, func, select, text, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.admin.schemas import AdminDashboard
from hmm_core.platform.settings import load_settings


async def dashboard(session: AsyncSession, now: datetime) -> AdminDashboard:
    settings = await load_settings(session)
    alert_counts = dict(
        (
            await session.execute(
                select(models.AdminAlert.type, func.count())
                .where(models.AdminAlert.status == AlertStatus.OPEN.value)
                .group_by(models.AdminAlert.type)
            )
        ).all()
    )
    pending_member_reviews = await session.scalar(
        select(func.count(models.MemberReview.user_id)).where(
            models.MemberReview.status == MemberReviewStatus.PENDING.value
        )
    )
    import_review_queue = await session.scalar(
        select(func.count(models.Event.id)).where(func.cardinality(models.Event.review_reasons) > 0)
    )
    member_event_duplicates = await session.scalar(
        select(func.count(models.Event.id)).where(
            models.Event.origin == EventOrigin.MEMBER.value,
            type_coerce(models.Event.review_reasons, ARRAY(Text)).contains(["possible_duplicate"]),
        )
    )
    to_check_items = await session.scalar(
        select(func.count(models.Flag.id)).where(models.Flag.status == FlagStatus.TO_CHECK.value)
    )
    active_restrictions = await session.scalar(
        select(func.count(models.MemberRestriction.id)).where(
            models.MemberRestriction.lifted_at.is_(None),
            models.MemberRestriction.until.is_(None) | (models.MemberRestriction.until > now),
        )
    )
    phase_counts = dict(
        (
            await session.execute(
                select(models.EventFormation.phase, func.count()).group_by(
                    models.EventFormation.phase
                )
            )
        ).all()
    )
    teams_without_lead = await session.scalar(
        select(func.count(models.Project.id)).where(
            models.Project.team_status.in_(["forming", "review", "locked", "active"]),
            models.Project.lead_user_id.is_(None),
        )
    )
    rows = (
        await session.execute(
            select(
                models.Project.team_status,
                models.Project.seats_closed_at,
                models.Event.team_size_min,
                models.Event.team_size_max,
                models.EventFormation.target_override,
                func.count(models.ProjectMember.id),
            )
            .join(models.Event, models.Event.id == models.Project.event_id)
            .outerjoin(
                models.EventFormation, models.EventFormation.event_id == models.Project.event_id
            )
            .outerjoin(
                models.ProjectMember,
                and_(
                    models.ProjectMember.project_id == models.Project.id,
                    models.ProjectMember.left_at.is_(None),
                ),
            )
            .where(models.Project.team_status.in_(["review", "locked", "active"]))
            .group_by(
                models.Project.id,
                models.Project.seats_closed_at,
                models.Event.team_size_min,
                models.Event.team_size_max,
                models.EventFormation.target_override,
            )
        )
    ).all()
    teams_below_min = 0
    open_seats = 0
    for team_status, seats_closed_at, min_override, max_override, target_override, size in rows:
        if size < (min_override or settings.team_size_default_min):
            teams_below_min += 1
        if team_status == "locked" and seats_closed_at is None:
            target = target_override or max_override or settings.team_size_default_max
            open_seats += max(0, target - size)
    results_to_verify = await session.scalar(
        select(func.count(models.ProjectResult.project_id)).where(
            models.ProjectResult.place_status == PlaceStatus.UNVERIFIED.value
        )
    )
    open_help_requests_without_offers = await session.scalar(
        select(func.count(models.HelpRequest.id)).where(
            models.HelpRequest.status == HelpRequestStatus.OPEN.value,
            ~exists().where(
                models.HelpOffer.request_id == models.HelpRequest.id,
                models.HelpOffer.status == HelpOfferStatus.PENDING.value,
            ),
        )
    )
    parser_last_heartbeat = await session.scalar(
        select(func.max(models.ParserHeartbeat.finished_at))
    )
    job_failures_24h = await session.scalar(
        text(
            "SELECT count(DISTINCT j.id) FROM procrastinate.procrastinate_jobs j "
            "JOIN procrastinate.procrastinate_events e ON e.job_id = j.id AND e.type = 'failed' "
            "WHERE j.status = 'failed' AND e.at >= :since"
        ),
        {"since": now - timedelta(hours=24)},
    )
    return AdminDashboard(
        open_alerts={kind: alert_counts.get(kind.value, 0) for kind in AlertKind},
        pending_member_reviews=pending_member_reviews or 0,
        import_review_queue=import_review_queue or 0,
        member_event_duplicates=member_event_duplicates or 0,
        to_check_items=to_check_items or 0,
        active_restrictions=active_restrictions or 0,
        hackathons_by_phase={phase: phase_counts.get(phase.value, 0) for phase in FormationPhase},
        teams_without_lead=teams_without_lead or 0,
        teams_below_min=teams_below_min,
        open_seats=open_seats,
        results_to_verify=results_to_verify or 0,
        open_help_requests_without_offers=open_help_requests_without_offers or 0,
        parser_last_heartbeat=parser_last_heartbeat,
        job_failures_24h=job_failures_24h or 0,
    )
