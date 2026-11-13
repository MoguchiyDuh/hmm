from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Self
from uuid import UUID

from hmm_contracts.algo import AlgoSettings, AreaScore, UnitScore
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType
from pydantic import Field, JsonValue, TypeAdapter, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.platform.audit import record_audit
from hmm_core.platform.models import Setting


class RuntimeSettings(AlgoSettings):
    registration_buffer_hours: int = Field(default=48, gt=0, description="Team deadline buffer")
    formation_lead_hours: int = Field(default=72, gt=0, description="Formation lead time")
    formation_min_notice_hours: int = Field(
        default=24, gt=0, description="Minimum formation notice"
    )
    review_hours: int = Field(default=48, gt=0, description="Formation review duration")
    review_min_hours: int = Field(default=12, gt=0, description="Minimum formation review duration")
    late_core_hours: int = Field(default=12, gt=0, description="Late core assembly duration")
    late_review_hours: int = Field(default=24, gt=0, description="Late formation review duration")
    seat_fill_hours: int = Field(default=12, gt=0, description="Lead seat-fill head start")
    late_claim_grace_hours: int = Field(default=24, gt=0, description="Late lead claim grace")
    presence_check_hours: int = Field(default=48, gt=0, description="Presence check lead time")
    presence_confirm_hours: int = Field(default=12, gt=0, description="Presence response deadline")
    lead_offer_hours: int = Field(default=12, gt=0, description="Lead offer expiry")
    reserve_offer_hours: int = Field(default=12, gt=0, description="Reserve offer expiry")
    confirm_reminder_hours: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=lambda: [24, 6, 1], description="Confirmation reminder lead times"
    )
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
    project_lead_offer_days: int = Field(default=3, gt=0, description="Project lead offer expiry")
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
        if self.formation_min_notice_hours > self.formation_lead_hours:
            raise ValueError("formation_min_notice_hours must not exceed formation_lead_hours")
        if self.review_min_hours > self.review_hours:
            raise ValueError("review_min_hours must not exceed review_hours")
        if self.presence_confirm_hours >= self.presence_check_hours:
            raise ValueError("presence_confirm_hours must be below presence_check_hours")
        if self.results_due_days >= self.results_close_days:
            raise ValueError("results_due_days must be below results_close_days")
        reminders = self.confirm_reminder_hours
        if not reminders or any(later >= earlier for earlier, later in pairwise(reminders)):
            raise ValueError("confirm_reminder_hours must be non-empty and strictly descending")
        return self


class SettingKey(StrEnum):
    ACTIVITY_WINDOW_DAYS = "activity_window_days"
    LEAD_FIT_THRESHOLD = "lead_fit_threshold"
    VIABLE_RATIO = "viable_ratio"
    CORE_RATIO = "core_ratio"
    TEAM_SIZE_DEFAULT_MIN = "team_size_default_min"
    TEAM_SIZE_DEFAULT_MAX = "team_size_default_max"
    MENTOR_MIN_SCORE = "mentor_min_score"
    MENTOR_MIN_GAP = "mentor_min_gap"
    MENTOR_CAPACITY_DEFAULT = "mentor_capacity_default"
    MENTOR_NOTIFY_TOP_N = "mentor_notify_top_n"
    REGISTRATION_BUFFER_HOURS = "registration_buffer_hours"
    FORMATION_LEAD_HOURS = "formation_lead_hours"
    FORMATION_MIN_NOTICE_HOURS = "formation_min_notice_hours"
    REVIEW_HOURS = "review_hours"
    REVIEW_MIN_HOURS = "review_min_hours"
    LATE_CORE_HOURS = "late_core_hours"
    LATE_REVIEW_HOURS = "late_review_hours"
    SEAT_FILL_HOURS = "seat_fill_hours"
    LATE_CLAIM_GRACE_HOURS = "late_claim_grace_hours"
    PRESENCE_CHECK_HOURS = "presence_check_hours"
    PRESENCE_CONFIRM_HOURS = "presence_confirm_hours"
    LEAD_OFFER_HOURS = "lead_offer_hours"
    RESERVE_OFFER_HOURS = "reserve_offer_hours"
    CONFIRM_REMINDER_HOURS = "confirm_reminder_hours"
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
    PROJECT_LEAD_OFFER_DAYS = "project_lead_offer_days"
    MENTEE_ACTIVE_MAX = "mentee_active_max"
    HELP_REQUEST_TTL_DAYS = "help_request_ttl_days"
    MENTORSHIP_IDLE_DAYS = "mentorship_idle_days"
    CONFIRMATION_WITHDRAW_DAYS = "confirmation_withdraw_days"


def apply_setting(settings: RuntimeSettings, key: SettingKey, value: JsonValue) -> RuntimeSettings:
    return RuntimeSettings.model_validate({**settings.model_dump(), key.value: value}, strict=True)


def setting_value_schema(key: SettingKey) -> dict[str, JsonValue]:
    return TypeAdapter(RuntimeSettings.model_fields[key.value].rebuild_annotation()).json_schema()


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    rows = await session.execute(select(Setting.key, Setting.value))
    return RuntimeSettings.model_validate({key: value for key, value in rows})


async def set_setting(
    session: AsyncSession,
    key: SettingKey,
    value: JsonValue,
    actor: UUID,
    reason: str | None = None,
) -> RuntimeSettings:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('settings'))"))
    before = await load_settings(session)
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
    return after
