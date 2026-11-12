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
from pydantic import AwareDatetime, ConfigDict, Field

type ScoreValue = Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)]


class ApiModel(Contract):
    pass


class ApiInput(Contract):
    model_config = ConfigDict(extra="forbid")


class WithAllowedActions(ApiModel):
    allowed_actions: list[str]


class UserRef(ApiModel):
    id: UserId
    display_name: str
    avatar_url: str | None


class AreaRef(ApiModel):
    key: AreaKey
    name: str


class AreaScoreSummary(ApiModel):
    area_key: AreaKey
    effective: ScoreValue
    reason: str


class EventRef(ApiModel):
    timezone: TimeZoneName
    id: EventId
    title: str
    kind: EventKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class ProjectRef(ApiModel):
    id: ProjectId
    name: str
    event_id: EventId | None
    team_status: TeamStatus | None


class ReasonBody(ApiInput):
    reason: str | None = None
