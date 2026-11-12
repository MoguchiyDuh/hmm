from hmm_contracts.common import (
    AreaKey,
    Contract,
    EventId,
    EventKind,
    ProjectId,
    TeamStatus,
    UserId,
)
from pydantic import AwareDatetime, ConfigDict


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


class EventRef(ApiModel):
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


class Reason(ApiInput):
    reason: str | None = None
