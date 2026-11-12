from enum import StrEnum
from typing import Annotated

from hmm_contracts.algo import AlgoSettings, AreaScore, UnitScore
from pydantic import Field, JsonValue, TypeAdapter


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


SettingKey = StrEnum("SettingKey", {key.upper(): key for key in RuntimeSettings.model_fields})

_ADAPTERS: dict[SettingKey, TypeAdapter[JsonValue]] = {
    SettingKey(key): TypeAdapter(field.rebuild_annotation())
    for key, field in RuntimeSettings.model_fields.items()
}


def validate_setting_value(key: SettingKey, value: JsonValue) -> JsonValue:
    return _ADAPTERS[key].validate_python(value, strict=True)


def setting_value_schema(key: SettingKey) -> dict[str, JsonValue]:
    return _ADAPTERS[key].json_schema()
