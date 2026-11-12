from hmm_contracts.common import AreaId, AreaKey
from pydantic import AwareDatetime

from hmm_core.api.schemas.common import ApiInput, AreaRef, WithAllowedActions


class Area(AreaRef, WithAllowedActions):
    id: AreaId
    description: str
    sort: int
    archived_at: AwareDatetime | None


class CreateAreaBody(ApiInput):
    key: AreaKey
    name: str
    description: str
    sort: int
    reason: str | None = None


class UpdateAreaBody(ApiInput):
    name: str | None = None
    description: str | None = None
    sort: int | None = None
    reason: str | None = None
