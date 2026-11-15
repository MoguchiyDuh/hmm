from fastapi import APIRouter, status

from hmm.common import HelpRequestId, ProjectId
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.jobs import Jobs
from hmm.platform.security import MemberAuth
from hmm.recommendations import service
from hmm.recommendations.schemas import ProjectRecommendations, Recommendation

router = APIRouter(tags=["recommendations"], responses=PROBLEM_RESPONSES)


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
    id: ProjectId, actor: MemberAuth, session: Session, jobs: Jobs, clock: CurrentClock
) -> None:
    await service.request_refresh(session, jobs, id, actor, clock.now())


@router.get(
    "/help-requests/{id}/suggested-mentors",
    status_code=status.HTTP_200_OK,
    summary="List suggested mentors as learner",
)
async def list_suggested_mentors(
    id: HelpRequestId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[Recommendation]:
    return await service.list_suggested_mentors(session, actor, id, clock.now())
