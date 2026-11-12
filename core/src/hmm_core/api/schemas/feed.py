from datetime import date
from enum import StrEnum

from hmm_contracts.algo import MentorshipRole
from hmm_contracts.common import (
    ActivityType,
    AreaId,
    AttendanceRole,
    ConfirmationFactType,
    ConfirmationId,
    ConfirmationSource,
    FactId,
    FactType,
    FeedItemId,
    FlagStatus,
    MemberRole,
    MentorshipOutcome,
    ProjectStatus,
    ResultOutcome,
    SubjectId,
    Trust,
    UserId,
)
from pydantic import AwareDatetime, Field, HttpUrl, JsonValue

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    AreaRef,
    EventRef,
    ProjectRef,
    UserRef,
    WithAllowedActions,
)


class FeedKind(StrEnum):
    PROJECT_RECRUITING = "project_recruiting"
    PROJECT_SHIPPED = "project_shipped"
    HELP_REQUEST = "help_request"
    EVENT_GOING = "event_going"
    EVENT_NEW = "event_new"
    MENTORSHIP_COMPLETED = "mentorship_completed"


class FeedItem(WithAllowedActions):
    id: FeedItemId
    kind: FeedKind
    subject_type: str
    subject_id: SubjectId
    actor_id: UserId | None
    actor: UserRef | None
    area_ids: list[AreaId]
    payload: dict[str, JsonValue]
    created_at: AwareDatetime
    hidden_at: AwareDatetime | None


class CreateConfirmationBody(ApiInput):
    fact_type: ConfirmationFactType
    fact_id: FactId


class Confirmation(WithAllowedActions):
    id: ConfirmationId
    fact_type: ConfirmationFactType
    fact_id: FactId
    subject_user_id: UserId
    confirmer_id: UserId | None
    source: ConfirmationSource
    ignored: bool
    created_at: AwareDatetime
    withdrawn_at: AwareDatetime | None


class FactAward(ApiModel):
    fact_type: FactType
    fact_id: FactId
    user_id: UserId
    xp: float
    activity: float
    reason: str
    algo_version: str
    computed_at: AwareDatetime


class Fact(WithAllowedActions):
    id: FactId
    type: FactType
    user_id: UserId
    subject_type: str
    subject_id: SubjectId
    title: str
    description_md: str | None
    activity_type: ActivityType | None
    links: list[HttpUrl]
    proof_links: list[HttpUrl] = Field(max_length=5)
    note: str | None
    occurred_on: date | None
    event: EventRef | None
    project: ProjectRef | None
    role: AttendanceRole | MemberRole | MentorshipRole | None
    area: AreaRef | None
    trust: Trust
    flag_status: FlagStatus | None
    confirmations: list[Confirmation]
    award: FactAward | None
    created_at: AwareDatetime
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    confirmed_at: AwareDatetime | None
    left_at: AwareDatetime | None
    outcome: ResultOutcome | ProjectStatus | MentorshipOutcome | None


class PendingConfirmationGroup(ApiModel):
    subject_type: str
    subject_id: SubjectId
    title: str
    facts: list[Fact]
