from hmm_contracts.common import AreaId
from pydantic import AwareDatetime

from hmm_core.api.schemas.common import AreaRef, WithAllowedActions


class AreaResponse(AreaRef, WithAllowedActions):
    id: AreaId
    description: str
    sort: int
    archived_at: AwareDatetime | None
