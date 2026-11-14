from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.common import (
    ActorKind,
    AlertStatus,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    EventOrigin,
    FlagStatus,
    FormationPhase,
    HelpOfferStatus,
    HelpRequestStatus,
    MemberReviewStatus,
    PlaceStatus,
)
from hmm_contracts.notifications import AlertKind
from pydantic import JsonValue, ValidationError
from sqlalchemy import Text, and_, exists, func, or_, select, text, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.admin.schemas import (
    AdminDashboard,
    AuditEntry,
    AuditEntryPage,
    Job,
    JobAction,
    JobStatus,
    QueueStats,
    Setting,
    SettingAction,
    StoredParserHeartbeat,
    SystemStatus,
)
from hmm_core.notifications.service import raise_alert, resolve_alerts
from hmm_core.platform.algo import load_algorithm
from hmm_core.platform.audit import record_audit
from hmm_core.platform.config import Config
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor, keyset_page
from hmm_core.platform.settings import (
    RuntimeSettings,
    SettingKey,
    load_settings,
    set_setting,
    setting_value_schema,
)

JOB_SELECT = (
    "SELECT j.id, j.task_name, j.queue_name, j.status::text AS status, j.attempts, "
    "min(e.at) AS created_at, max(e.at) AS updated_at "
    "FROM procrastinate.procrastinate_jobs j "
    "JOIN procrastinate.procrastinate_events e ON e.job_id = j.id "
)
JOB_GROUP_ORDER = (
    " GROUP BY j.id, j.task_name, j.queue_name, j.status, j.attempts "
    "ORDER BY max(e.at) DESC, j.id DESC"
)


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


async def list_users(
    session: AsyncSession,
    pagination: CursorParams,
    q: str | None = None,
    banned: bool | None = None,
    is_admin: bool | None = None,
) -> tuple[list[models.User], str | None]:
    statement = select(models.User)
    if q:
        statement = statement.where(
            or_(models.User.display_name.ilike(f"%{q}%"), models.User.github_login.ilike(f"%{q}%"))
        )
    if banned is True:
        statement = statement.where(models.User.banned_at.is_not(None))
    elif banned is False:
        statement = statement.where(models.User.banned_at.is_(None))
    if is_admin is not None:
        statement = statement.where(models.User.is_admin.is_(is_admin))
    rows = list(
        await session.scalars(
            apply_keyset(
                statement,
                pagination,
                sort=[models.User.created_at],
                id_column=models.User.id,
            )
        )
    )
    items = rows[: pagination.limit]
    next_cursor = (
        encode_cursor([items[-1].created_at, items[-1].id])
        if len(rows) > pagination.limit
        else None
    )
    return items, next_cursor


async def list_audit_entries(
    session: AsyncSession,
    pagination: CursorParams,
    actor_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
) -> AuditEntryPage:
    statement = select(models.AuditLog)
    if actor_id is not None:
        statement = statement.where(models.AuditLog.actor_id == actor_id)
    if entity_type is not None:
        statement = statement.where(models.AuditLog.entity_type == entity_type)
    if entity_id is not None:
        statement = statement.where(models.AuditLog.entity_id == str(entity_id))
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.AuditLog.created_at],
        id_column=models.AuditLog.id,
        serialize=AuditEntry.model_validate,
    )
    return AuditEntryPage(items=page.items, next_cursor=page.next_cursor)


def setting_contract(
    key: SettingKey, value: JsonValue, default: JsonValue, row: models.Setting | None
) -> Setting:
    return Setting(
        key=key,
        value=value,
        default=default,
        description=RuntimeSettings.model_fields[key.value].description or "",
        value_schema=setting_value_schema(key),
        updated_by=row.updated_by if row else None,
        updated_at=row.updated_at if row else None,
        allowed_actions=[SettingAction.EDIT],
    )


async def list_settings(session: AsyncSession) -> list[Setting]:
    defaults = RuntimeSettings().model_dump(mode="json")
    values = (await load_settings(session)).model_dump(mode="json")
    rows = {row.key: row for row in await session.scalars(select(models.Setting))}
    return [
        setting_contract(key, values[key.value], defaults[key.value], rows.get(key.value))
        for key in SettingKey
    ]


async def update_setting(
    session: AsyncSession,
    key: SettingKey,
    value: JsonValue,
    actor: UUID,
    reason: str | None = None,
) -> Setting:
    try:
        updated = await set_setting(session, key, value, actor, reason)
    except ValidationError as error:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, str(error)) from error
    row = (
        await session.execute(
            select(models.Setting)
            .where(models.Setting.key == key.value)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return setting_contract(
        key,
        updated.model_dump(mode="json")[key.value],
        RuntimeSettings().model_dump(mode="json")[key.value],
        row,
    )


async def check_parser_down(
    session: AsyncSession, now: datetime, settings: RuntimeSettings
) -> None:
    last = await session.scalar(select(func.max(models.ParserHeartbeat.finished_at)))
    if last is None:
        return
    if now - last > timedelta(hours=settings.parser_heartbeat_max_age_hours):
        await raise_alert(
            session,
            AlertKind.PARSER_DOWN,
            now,
            title="Parser is down",
            message=f"No parser heartbeat since {last.isoformat()}",
            link="/admin/system",
            subject_type="parser",
            subject_title="Parser",
            dedupe_key="parser_down",
        )
    else:
        await resolve_alerts(session, "parser_down", now)


async def system_status(session: AsyncSession, config: Config, now: datetime) -> SystemStatus:
    heartbeats = await session.scalars(
        select(models.ParserHeartbeat)
        .where(models.ParserHeartbeat.finished_at.is_not(None))
        .order_by(models.ParserHeartbeat.started_at.desc())
        .limit(20)
    )
    queues = await session.execute(
        text(
            "SELECT queue_name, count(*) FILTER (WHERE status = 'todo') AS pending, "
            "count(*) FILTER (WHERE status = 'doing') AS running, "
            "count(*) FILTER (WHERE status = 'failed') AS failed "
            "FROM procrastinate.procrastinate_jobs GROUP BY queue_name ORDER BY queue_name"
        )
    )
    return SystemStatus(
        parser_heartbeats=[StoredParserHeartbeat.model_validate(row) for row in heartbeats],
        job_queue_stats=[QueueStats.model_validate(row) for row in queues],
        algo_version=load_algorithm(config).version,
        failed_jobs=await job_views(session, failed_only=True),
    )


async def job_contract(session: AsyncSession, row: RowMapping) -> Job:
    payload = await session.scalar(
        select(models.AdminAlert.payload)
        .where(
            models.AdminAlert.type == AlertKind.JOB_FAILING.value,
            text("payload->>'job_id' = :job_id"),
        )
        .params(job_id=str(row["id"]))
        .order_by(models.AdminAlert.created_at.desc())
        .limit(1)
    )
    error = payload.get("error") if isinstance(payload, dict) else None
    status = JobStatus(row["status"])
    return Job(
        id=row["id"],
        task_name=JobName(row["task_name"]),
        queue_name=row["queue_name"],
        status=status,
        attempts=row["attempts"],
        last_error=error if isinstance(error, str) else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        allowed_actions=[JobAction.RETRY] if status == JobStatus.FAILED else [],
    )


async def job_view(session: AsyncSession, job_id: int) -> Job:
    row = (
        (
            await session.execute(
                text(JOB_SELECT + "WHERE j.id = :job_id" + JOB_GROUP_ORDER), {"job_id": job_id}
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row["task_name"] not in JobName:
        raise ProblemError(ErrorCode.INVALID_STATE, "Unknown job task")
    return await job_contract(session, row)


async def job_views(session: AsyncSession, failed_only: bool = True) -> list[Job]:
    where = "WHERE j.status = 'failed'" if failed_only else ""
    rows = await session.execute(text(JOB_SELECT + where + JOB_GROUP_ORDER + " LIMIT 50"))
    return [
        await job_contract(session, row) for row in rows.mappings() if row["task_name"] in JobName
    ]


async def retry_job(
    session: AsyncSession, job_id: int, actor: UUID, now: datetime, reason: str | None
) -> Job:
    row = (
        await session.execute(
            text(
                "SELECT task_name, status::text AS status "
                "FROM procrastinate.procrastinate_jobs WHERE id = :job_id"
            ),
            {"job_id": job_id},
        )
    ).one_or_none()
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.status != "failed":
        raise ProblemError(ErrorCode.INVALID_STATE, "Only failed jobs can be retried")
    if row.task_name not in JobName:
        raise ProblemError(ErrorCode.INVALID_STATE, "Unknown job task")
    await session.execute(
        text(
            "UPDATE procrastinate.procrastinate_jobs SET status = 'todo', attempts = 0, "
            "scheduled_at = :now, queueing_lock = NULL "
            "WHERE id = :job_id AND status = 'failed'"
        ),
        {"now": now, "job_id": job_id},
    )
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.JOB_RETRY,
        entity_type=AuditEntityType.JOB,
        entity_id=str(job_id),
        before={"status": "failed"},
        after={"status": "todo"},
        reason=reason,
    )
    return await job_view(session, job_id)
