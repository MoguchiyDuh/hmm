from enum import StrEnum

from pydantic import AwareDatetime

from hmm.algo.models import Rec
from hmm.common import RecommendationStatus
from hmm.platform.schemas import ApiModel, UserRef, WithAllowedActions


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
