from typing import Annotated

from fastapi import APIRouter, Depends, status
from hmm_contracts.common import HelpRequestId, ProjectId

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.schemas.recommendations import ProjectRecommendations, Recommendation
from hmm_core.api.security import member

router = APIRouter(tags=["recommendations"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["recommendations"], responses=PROBLEM_RESPONSES)


@router.get(
    "/projects/{id}/recommendations",
    status_code=status.HTTP_200_OK,
    summary="Get quick-pick recommendations as lead or admin",
)
async def get_project_recommendations(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> ProjectRecommendations:
    not_implemented()


@router.post(
    "/projects/{id}/recommendations/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Refresh recommendations as lead, at most once per minute",
)
async def refresh_project_recommendations(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> None:
    not_implemented()


@router.get(
    "/help-requests/{id}/suggested-mentors",
    status_code=status.HTTP_200_OK,
    summary="List suggested mentors as learner",
)
async def list_suggested_mentors(
    id: HelpRequestId, actor: Annotated[None, Depends(member)]
) -> list[Recommendation]:
    not_implemented()
