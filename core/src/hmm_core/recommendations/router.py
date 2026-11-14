from fastapi import APIRouter, Request, status
from hmm_contracts.common import HelpRequestId, ProjectId

from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES
from hmm_core.platform.security import MemberAuth
from hmm_core.recommendations import service
from hmm_core.recommendations.schemas import ProjectRecommendations, Recommendation

router = APIRouter(tags=["recommendations"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["recommendations"], responses=PROBLEM_RESPONSES)


@router.get(
    "/projects/{id}/recommendations",
    status_code=status.HTTP_200_OK,
    summary="Get quick-pick recommendations as lead or admin",
)
async def get_project_recommendations(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> ProjectRecommendations:
    return await service.project_recommendations(session, id, actor, clock.now())


@router.post(
    "/projects/{id}/recommendations/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Refresh recommendations as lead, at most once per minute",
)
async def refresh_project_recommendations(
    id: ProjectId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> None:
    await service.request_refresh(session, request.app.state.jobs, id, actor, clock.now())


@router.get(
    "/help-requests/{id}/suggested-mentors",
    status_code=status.HTTP_200_OK,
    summary="List suggested mentors as learner",
)
async def list_suggested_mentors(
    id: HelpRequestId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[Recommendation]:
    return await service.list_suggested_mentors(session, actor, id, clock.now())
