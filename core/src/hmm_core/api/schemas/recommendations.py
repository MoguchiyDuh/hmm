from hmm_contracts.algo import Rec
from hmm_contracts.common import RecommendationStatus
from pydantic import AwareDatetime

from hmm_core.api.schemas.common import ApiModel, UserRef, WithAllowedActions


class Recommendation(Rec, ApiModel):
    profile: UserRef


class ProjectRecommendations(WithAllowedActions):
    status: RecommendationStatus
    stale: bool
    computed_at: AwareDatetime | None
    enrolled: list[Recommendation]
    others: list[Recommendation]
