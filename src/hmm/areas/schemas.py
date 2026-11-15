from enum import StrEnum

from pydantic import AwareDatetime

from hmm.common import AreaId, AreaKey
from hmm.platform.schemas import (
    ApiModel,
    AreaRef,
    Name,
    Note,
    WithAllowedActions,
)


class AreaAction(StrEnum):
    EDIT = "edit"
    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"


class Area(AreaRef, WithAllowedActions[AreaAction]):
    id: AreaId
    description: str
    sort: int
    archived_at: AwareDatetime | None


class CreateAreaBody(ApiModel):
    key: AreaKey
    name: Name
    description: Note
    sort: int
    reason: Note | None = None


class UpdateAreaBody(ApiModel):
    name: Name | None = None
    description: Note | None = None
    sort: int | None = None
    reason: Note | None = None
