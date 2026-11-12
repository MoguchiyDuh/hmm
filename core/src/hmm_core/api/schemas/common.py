from enum import StrEnum
from typing import Annotated

from hmm_contracts.common import (
    AreaKey,
    Contract,
    EventId,
    EventKind,
    ProjectId,
    TeamStatus,
    TimeZoneName,
    UserId,
)
from pydantic import AwareDatetime, Field, HttpUrl, StringConstraints

type ScoreValue = Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)]
type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
type Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
type Markdown = Annotated[str, StringConstraints(max_length=5000)]
type LongMarkdown = Annotated[str, StringConstraints(max_length=20000)]
type Links = Annotated[list[HttpUrl], Field(max_length=10)]
type Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]


class EventAction(StrEnum):
    GOING = "going"
    ATTENDED = "attended"
    ENROLL = "enroll"
    WITHDRAW = "withdraw"
    CLAIM_LEAD = "claim_lead"
    REQUEST_JOIN = "request_join"
    EDIT = "edit"
    CANCEL = "cancel"
    PUBLISH = "publish"
    REJECT = "reject"
    MERGE = "merge"


class ApiModel(Contract):
    pass


class ApiInput(Contract):
    pass


class WithAllowedActions[A: StrEnum](ApiModel):
    allowed_actions: list[A]


class UserRef(ApiModel):
    id: UserId
    display_name: str
    avatar_url: HttpUrl | None


class AreaRef(ApiModel):
    key: AreaKey
    name: str


class AreaScoreSummary(ApiModel):
    area_key: AreaKey
    effective: ScoreValue
    reason: str


class EventRef(ApiModel):
    id: EventId
    title: str
    kind: EventKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    timezone: TimeZoneName


class ProjectRef(ApiModel):
    id: ProjectId
    name: str
    event_id: EventId | None
    team_status: TeamStatus | None


class ReasonBody(ApiInput):
    reason: Note | None = None
