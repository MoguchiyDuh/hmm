from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, JsonValue, TypeAdapter, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm.algo.models import AlgoSettings, AreaScore, UnitScore
from hmm.common import ActorKind, AuditAction, AuditEntityType
from hmm.platform.audit import record_audit
from hmm.platform.models import Setting


class RuntimeSettings(AlgoSettings):
    formation_lead_hours: int = Field(default=72, gt=0, description="Formation lead time")
    accept_hours: int = Field(default=24, gt=0, description="Placement acceptance duration")
    invite_ttl_days: int = Field(default=7, gt=0, description="Invite expiry before phase caps")
    results_due_days: int = Field(default=7, gt=0, description="Member result submission delay")
    results_close_days: int = Field(default=30, gt=0, description="Automatic result closure delay")
    review_window_days: int = Field(default=7, gt=0, description="Blind review window")
    score_change_notify_delta: AreaScore = Field(
        default=1.0, description="Score notification delta"
    )
    member_review_reminder_days: int = Field(default=7, gt=0, description="Member review reminder")
    import_auto_publish_confidence: UnitScore = Field(
        default=0.8, description="Import auto-publish confidence"
    )
    parser_heartbeat_max_age_hours: int = Field(
        default=26, gt=0, description="Parser silence alert"
    )
    session_ttl_days: int = Field(default=30, gt=0, description="Web session lifetime")
    github_manual_refresh_min_interval_minutes: int = Field(
        default=60, gt=0, description="Manual GitHub refresh interval"
    )
    cv_max_bytes: int = Field(default=5_242_880, gt=0, description="CV upload size limit")
    event_archive_after_days: int = Field(default=180, gt=0, description="Event archival delay")
    attendance_claim_days: int = Field(default=30, gt=0, description="Attendance claim window")
    project_size_max: int = Field(default=20, ge=1, description="Standalone project member limit")
    projects_led_max: int = Field(
        default=5, ge=1, description="Standalone project leadership limit"
    )
    project_idle_days: int = Field(default=30, gt=0, description="Project inactivity threshold")
    project_review_min_days: int = Field(
        default=3, gt=0, description="Minimum project review tenure"
    )
    mentee_active_max: int = Field(
        default=2, ge=1, description="Concurrent learner mentorship limit"
    )
    help_request_ttl_days: int = Field(
        default=14, gt=0, description="Unmatched help request expiry"
    )
    mentorship_idle_days: int = Field(
        default=14, gt=0, description="Mentorship inactivity threshold"
    )
    confirmation_withdraw_days: int = Field(
        default=7, gt=0, description="Confirmation withdrawal window"
    )

    @model_validator(mode="after")
    def validate_related_settings(self) -> Self:
        if self.results_due_days >= self.results_close_days:
            raise ValueError("results_due_days must be below results_close_days")
        return self


class SettingKey(StrEnum):
    ACTIVITY_WINDOW_DAYS = "activity_window_days"
    TEAM_SIZE_DEFAULT_MIN = "team_size_default_min"
    TEAM_SIZE_DEFAULT_MAX = "team_size_default_max"
    MENTOR_MIN_SCORE = "mentor_min_score"
    MENTOR_MIN_GAP = "mentor_min_gap"
    MENTOR_CAPACITY_DEFAULT = "mentor_capacity_default"
    MENTOR_NOTIFY_TOP_N = "mentor_notify_top_n"
    FORMATION_LEAD_HOURS = "formation_lead_hours"
    ACCEPT_HOURS = "accept_hours"
    INVITE_TTL_DAYS = "invite_ttl_days"
    RESULTS_DUE_DAYS = "results_due_days"
    RESULTS_CLOSE_DAYS = "results_close_days"
    REVIEW_WINDOW_DAYS = "review_window_days"
    SCORE_CHANGE_NOTIFY_DELTA = "score_change_notify_delta"
    MEMBER_REVIEW_REMINDER_DAYS = "member_review_reminder_days"
    IMPORT_AUTO_PUBLISH_CONFIDENCE = "import_auto_publish_confidence"
    PARSER_HEARTBEAT_MAX_AGE_HOURS = "parser_heartbeat_max_age_hours"
    SESSION_TTL_DAYS = "session_ttl_days"
    GITHUB_MANUAL_REFRESH_MIN_INTERVAL_MINUTES = "github_manual_refresh_min_interval_minutes"
    CV_MAX_BYTES = "cv_max_bytes"
    EVENT_ARCHIVE_AFTER_DAYS = "event_archive_after_days"
    ATTENDANCE_CLAIM_DAYS = "attendance_claim_days"
    PROJECT_SIZE_MAX = "project_size_max"
    PROJECTS_LED_MAX = "projects_led_max"
    PROJECT_IDLE_DAYS = "project_idle_days"
    PROJECT_REVIEW_MIN_DAYS = "project_review_min_days"
    MENTEE_ACTIVE_MAX = "mentee_active_max"
    HELP_REQUEST_TTL_DAYS = "help_request_ttl_days"
    MENTORSHIP_IDLE_DAYS = "mentorship_idle_days"
    CONFIRMATION_WITHDRAW_DAYS = "confirmation_withdraw_days"


def apply_setting(settings: RuntimeSettings, key: SettingKey, value: JsonValue) -> RuntimeSettings:
    return RuntimeSettings.model_validate({**settings.model_dump(), key.value: value}, strict=True)


def setting_value_schema(key: SettingKey) -> dict[str, JsonValue]:
    return TypeAdapter(RuntimeSettings.model_fields[key.value].rebuild_annotation()).json_schema()


async def read_settings(session: AsyncSession) -> RuntimeSettings:
    rows = await session.execute(select(Setting.key, Setting.value))
    return _cache(session, RuntimeSettings.model_validate({key: value for key, value in rows}))


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    match session.info.get(RuntimeSettings):
        case (transaction, settings) if transaction is session.sync_session.get_transaction():
            return settings
        case _:
            return await read_settings(session)


def _cache(session: AsyncSession, settings: RuntimeSettings) -> RuntimeSettings:
    session.info[RuntimeSettings] = (session.sync_session.get_transaction(), settings)
    return settings


async def set_setting(
    session: AsyncSession,
    key: SettingKey,
    value: JsonValue,
    actor: UUID,
    reason: str | None = None,
) -> RuntimeSettings:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('settings'))"))
    before = await read_settings(session)
    after = apply_setting(before, key, value)
    normalized = after.model_dump(mode="json")[key.value]
    statement = insert(Setting).values(key=key.value, value=normalized, updated_by=actor)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": normalized, "updated_by": actor, "updated_at": text("now()")},
        )
    )
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.SETTING_SET,
        entity_type=AuditEntityType.SETTING,
        entity_id=key.value,
        before=before.model_dump(mode="json")[key.value],
        after=normalized,
        reason=reason,
    )
    return _cache(session, after)
