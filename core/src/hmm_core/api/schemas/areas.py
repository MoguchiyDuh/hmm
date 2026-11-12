from enum import StrEnum

from hmm_contracts.common import AreaId, AreaKey
from pydantic import AwareDatetime

from hmm_core.api.schemas.common import (
    ApiInput,
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


class CreateAreaBody(ApiInput):
    key: AreaKey
    name: Name
    description: Note
    sort: int
    reason: Note | None = None


class UpdateAreaBody(ApiInput):
    name: Name | None = None
    description: Note | None = None
    sort: int | None = None
    reason: Note | None = None
