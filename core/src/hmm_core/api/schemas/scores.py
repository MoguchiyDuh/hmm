from datetime import date, timedelta
from typing import Literal

from hmm_contracts.algo import RatingSummary
from hmm_contracts.common import (
    ActivityEntryId,
    ActivityType,
    AreaId,
    AreaScoreEventId,
    AreaScoreEventKind,
    FlagId,
    FlagStatus,
    FlagSubjectType,
    OverrideEvidence,
    RestrictionId,
    RestrictionKind,
    ReviewContext,
    Severity,
    SubjectId,
    Trust,
    UserId,
    XpAdjustmentId,
)
from pydantic import AwareDatetime, Field, HttpUrl, JsonValue, field_validator

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    AreaScoreSummary,
    ScoreValue,
    UserRef,
    WithAllowedActions,
)


class MemberAreaScore(AreaScoreSummary, WithAllowedActions):
    area_id: AreaId
    calculated: ScoreValue | None = None
    calc_reason: str | None = None
    override: ScoreValue | None = None
    override_by: UserId | None = None
    override_reason: str | None = None
    override_evidence: OverrideEvidence | None = None
    override_at: AwareDatetime | None = None


class AreaScoreEvent(ApiModel):
    id: AreaScoreEventId
    user_id: UserId
    area_id: AreaId
    kind: AreaScoreEventKind
    old_effective: ScoreValue
    new_effective: ScoreValue
    reason: str
    algo_version: str | None
    actor_id: UserId | None
    created_at: AwareDatetime


class CreateActivityBody(ApiInput):
    type: ActivityType
    title: str = Field(max_length=200)
    description_md: str = Field(max_length=5000)
    links: list[HttpUrl] = Field(max_length=10)
    area_id: AreaId | None = None
    occurred_on: date

    @field_validator("occurred_on")
    @classmethod
    def validate_occurred_on(cls, value: date) -> date:
        if not date.today() - timedelta(days=365) <= value <= date.today():
            raise ValueError("Activity date must be within the past 365 days")
        return value


class UpdateActivityBody(ApiInput):
    type: ActivityType | None = None
    title: str | None = Field(default=None, max_length=200)
    description_md: str | None = Field(default=None, max_length=5000)
    links: list[HttpUrl] | None = Field(default=None, max_length=10)
    area_id: AreaId | None = None
    occurred_on: date | None = None

    @field_validator("occurred_on")
    @classmethod
    def validate_occurred_on(cls, value: date | None) -> date | None:
        if value is not None:
            return CreateActivityBody.validate_occurred_on(value)
        return value


class ActivityEntry(WithAllowedActions):
    id: ActivityEntryId
    user_id: UserId
    type: ActivityType
    title: str
    description_md: str
    links: list[HttpUrl]
    area_id: AreaId | None
    occurred_on: date
    trust: Literal[Trust.SELF] = Trust.SELF
    created_at: AwareDatetime
    updated_at: AwareDatetime


class MemberMetrics(ApiModel):
    xp: float
    activity: float = Field(ge=0)
    reputation: dict[ReviewContext, RatingSummary]
    hackathons_finished: int = Field(ge=0)
    projects_shipped: int = Field(ge=0)
    mentorships_completed: int = Field(ge=0)


class SetScoreOverrideBody(ApiInput):
    value: ScoreValue
    reason: str
    evidence: OverrideEvidence


class CreateXpAdjustmentBody(ApiInput):
    amount: float = Field(allow_inf_nan=False)
    reason: str


class XpAdjustment(ApiModel):
    id: XpAdjustmentId
    user_id: UserId
    amount: float
    reason: str
    admin_id: UserId
    flag_id: FlagId | None
    created_at: AwareDatetime


class RestrictionPenalty(ApiInput):
    kind: RestrictionKind
    until: AwareDatetime | None


class XpPenalty(ApiInput):
    amount: float = Field(gt=0, allow_inf_nan=False)


class FlagPenalties(ApiInput):
    exclude: Literal[True] = True
    xp_penalty: XpPenalty | None = None
    revoke_confirmations: bool = False
    restrictions: list[RestrictionPenalty] = Field(default_factory=list)
    ban: bool = False


class ResolveFlagBody(ApiInput):
    decision: Literal["pardon", "punish"]
    penalties: FlagPenalties | None = None
    note: str
    reason: str | None = None


class Flag(WithAllowedActions):
    id: FlagId
    user_id: UserId
    subject_type: FlagSubjectType
    subject_id: SubjectId
    subject_content: dict[str, JsonValue]
    related_user_ids: list[UserId]
    related_members: list[UserRef]
    reason: str
    severity: Severity
    status: FlagStatus
    algo_version: str
    resolved_by: UserId | None
    resolved_at: AwareDatetime | None
    resolution_note: str | None
    penalty: FlagPenalties | None
    created_at: AwareDatetime


class Restriction(WithAllowedActions):
    id: RestrictionId
    user_id: UserId
    kind: RestrictionKind
    until: AwareDatetime | None
    reason: str
    flag_id: FlagId | None
    created_by: UserId
    lifted_at: AwareDatetime | None
    created_at: AwareDatetime


class LiftRestrictionBody(ApiInput):
    reason: str
