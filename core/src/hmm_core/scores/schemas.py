from datetime import date, timedelta
from enum import StrEnum
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
    Page,
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

from hmm_core.platform.schemas import (
    ApiModel,
    AreaScoreSummary,
    Links,
    Markdown,
    Note,
    ScoreValue,
    Title,
    UserRef,
    WithAllowedActions,
)


class AreaScoreAction(StrEnum):
    OVERRIDE = "override"
    CLEAR_OVERRIDE = "clear_override"


class ActivityEntryAction(StrEnum):
    EDIT = "edit"
    DELETE = "delete"


class FlagAction(StrEnum):
    RESOLVE = "resolve"


class RestrictionAction(StrEnum):
    LIFT = "lift"


class MemberAreaScore(AreaScoreSummary, WithAllowedActions[AreaScoreAction]):
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


class CreateActivityBody(ApiModel):
    type: ActivityType
    title: Title
    description_md: Markdown
    links: Links
    area_id: AreaId | None = None
    occurred_on: date

    @field_validator("occurred_on")
    @classmethod
    def validate_occurred_on(cls, value: date) -> date:
        if not date.today() - timedelta(days=365) <= value <= date.today():
            raise ValueError("Activity date must be within the past 365 days")
        return value


class UpdateActivityBody(ApiModel):
    type: ActivityType | None = None
    title: Title | None = None
    description_md: Markdown | None = None
    links: Links | None = None
    area_id: AreaId | None = None
    occurred_on: date | None = None

    @field_validator("occurred_on")
    @classmethod
    def validate_occurred_on(cls, value: date | None) -> date | None:
        if value is not None:
            return CreateActivityBody.validate_occurred_on(value)
        return value


class ActivityEntry(WithAllowedActions[ActivityEntryAction]):
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


class SetScoreOverrideBody(ApiModel):
    value: ScoreValue
    reason: Note
    evidence: OverrideEvidence


class CreateXpAdjustmentBody(ApiModel):
    amount: float = Field(allow_inf_nan=False)
    reason: Note


class XpAdjustment(ApiModel):
    id: XpAdjustmentId
    user_id: UserId
    amount: float
    reason: str
    admin_id: UserId
    flag_id: FlagId | None
    created_at: AwareDatetime


class RestrictionPenalty(ApiModel):
    kind: RestrictionKind
    until: AwareDatetime | None


class XpPenalty(ApiModel):
    amount: float = Field(gt=0, allow_inf_nan=False)


class FlagPenalties(ApiModel):
    exclude: Literal[True] = True
    xp_penalty: XpPenalty | None = None
    revoke_confirmations: bool = False
    restrictions: list[RestrictionPenalty] = Field(default_factory=list)
    ban: bool = False


class ResolveFlagBody(ApiModel):
    decision: Literal["pardon", "punish"]
    penalties: FlagPenalties | None = None
    note: Note
    reason: Note | None = None


class Flag(WithAllowedActions[FlagAction]):
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


class Restriction(WithAllowedActions[RestrictionAction]):
    id: RestrictionId
    user_id: UserId
    kind: RestrictionKind
    until: AwareDatetime | None
    reason: str
    flag_id: FlagId | None
    created_by: UserId
    lifted_at: AwareDatetime | None
    created_at: AwareDatetime


class LiftRestrictionBody(ApiModel):
    reason: Note


class ActivityEntryPage(Page[ActivityEntry]):
    pass


class AreaScoreEventPage(Page[AreaScoreEvent]):
    pass


class FlagPage(Page[Flag]):
    pass
