from hmm_contracts.common import (
    ActorKind,
    AuditLogId,
    FormationPhase,
    OnboardingStatus,
    ParserHeartbeatId,
    SubjectId,
    UserId,
)
from hmm_contracts.imports import ParserHeartbeat
from hmm_contracts.notifications import AlertKind
from pydantic import AwareDatetime, Field, JsonValue

from hmm_core.api.schemas.common import ApiModel, ReasonBody, UserRef, WithAllowedActions
from hmm_core.settings import SettingKey


class AdminUser(UserRef, WithAllowedActions):
    github_id: int
    github_login: str
    about_md: str = Field(max_length=5000)
    is_admin: bool
    banned_at: AwareDatetime | None
    ban_reason: str | None
    onboarding_status: OnboardingStatus
    last_seen_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UpdateUserBody(ReasonBody):
    is_admin: bool | None = None
    display_name: str | None = None
    about_md: str | None = Field(default=None, max_length=5000)


class BanUserBody(ReasonBody):
    reason: str = Field(min_length=1)


class UnbanUserBody(ReasonBody):
    pass


class AdminDashboard(ApiModel):
    open_alerts: dict[AlertKind, int]
    pending_member_reviews: int = Field(ge=0)
    import_review_queue: int = Field(ge=0)
    member_event_duplicates: int = Field(ge=0)
    to_check_items: int = Field(ge=0)
    active_restrictions: int = Field(ge=0)
    hackathons_by_phase: dict[FormationPhase, int]
    teams_without_lead: int = Field(ge=0)
    teams_below_min: int = Field(ge=0)
    open_seats: int = Field(ge=0)
    results_to_verify: int = Field(ge=0)
    open_help_requests_without_offers: int = Field(ge=0)
    parser_last_heartbeat: AwareDatetime | None
    job_failures_24h: int = Field(ge=0)


class AuditEntry(ApiModel):
    id: AuditLogId
    actor_id: UserId | None
    actor_kind: ActorKind
    action: str
    entity_type: str
    entity_id: SubjectId
    before: JsonValue
    after: JsonValue
    reason: str | None
    created_at: AwareDatetime


class Setting(WithAllowedActions):
    key: SettingKey
    value: JsonValue
    default: JsonValue
    description: str
    value_schema: dict[str, JsonValue] = Field(
        description="JSON schema for this setting key's value"
    )
    updated_by: UserId | None
    updated_at: AwareDatetime | None


class UpdateSettingBody(ReasonBody):
    value: JsonValue


class StoredParserHeartbeat(ParserHeartbeat, ApiModel):
    id: ParserHeartbeatId


class Job(WithAllowedActions):
    id: int
    task_name: str
    queue_name: str
    status: str
    attempts: int = Field(ge=0)
    last_error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class QueueStats(ApiModel):
    queue_name: str
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    failed: int = Field(ge=0)


class SystemStatus(ApiModel):
    parser_heartbeats: list[StoredParserHeartbeat]
    job_queue_stats: list[QueueStats]
    algo_version: str
    failed_jobs: list[Job]


class RetryJobBody(ReasonBody):
    pass
