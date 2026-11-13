from enum import StrEnum

from hmm_contracts.algo import Rec
from hmm_contracts.common import RecommendationStatus
from pydantic import AwareDatetime

from hmm_core.platform.schemas import ApiModel, UserRef, WithAllowedActions


class RecommendationsAction(StrEnum):
    REFRESH = "refresh"


class Recommendation(Rec, ApiModel):
    profile: UserRef


class ProjectRecommendations(WithAllowedActions[RecommendationsAction]):
    status: RecommendationStatus
    stale: bool
    computed_at: AwareDatetime | None
    enrolled: list[Recommendation]
    others: list[Recommendation]
