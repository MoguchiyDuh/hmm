from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status
from hmm_contracts.common import (
    AreaKey,
    EventId,
    MilestoneId,
    ProjectId,
    ProjectStatus,
    TeamStatus,
    UserId,
)

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.common import ReasonBody
from hmm_core.api.schemas.projects import (
    CreateMilestoneBody,
    CreateProjectBody,
    CreateProjectInviteBody,
    CreateProjectJoinRequestBody,
    CreateProjectReleaseBody,
    CreateProjectUpdateBody,
    Invite,
    JoinRequest,
    Milestone,
    Project,
    ProjectListing,
    ProjectPage,
    ProjectRelease,
    ProjectSummary,
    ProjectUpdate,
    UpdateMilestoneBody,
    UpdateProjectBody,
)
from hmm_core.api.security import member

router = APIRouter(tags=["projects"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["projects"], responses=PROBLEM_RESPONSES)


@router.get("/projects", status_code=status.HTTP_200_OK, summary="List projects")
async def list_projects(
    actor: Annotated[None, Depends(member)],
    pagination: Annotated[CursorParams, Depends()],
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    recruiting: Annotated[bool | None, Query()] = None,
    event: Annotated[EventId | None, Query()] = None,
    member_id: Annotated[UserId | None, Query(alias="member")] = None,
) -> ProjectPage:
    not_implemented()


@router.post(
    "/projects", status_code=status.HTTP_201_CREATED, summary="Create a standalone project"
)
async def create_project(
    body: CreateProjectBody, actor: Annotated[None, Depends(member)]
) -> Project:
    not_implemented()


@router.get(
    "/me/projects", status_code=status.HTTP_200_OK, summary="List own projects and hackathon teams"
)
async def list_my_projects(
    actor: Annotated[None, Depends(member)],
    project_status: Annotated[ProjectStatus | TeamStatus | None, Query(alias="status")] = None,
) -> list[ProjectSummary]:
    not_implemented()


@router.get(
    "/projects/{id}", status_code=status.HTTP_200_OK, summary="Get project respecting visibility"
)
async def get_project(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> Project | ProjectListing:
    not_implemented()


@router.patch("/projects/{id}", status_code=status.HTTP_200_OK, summary="Edit project as lead")
async def update_project(
    id: ProjectId, body: UpdateProjectBody, actor: Annotated[None, Depends(member)]
) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/start", status_code=status.HTTP_200_OK, summary="Start project as lead"
)
async def start_project(id: ProjectId, actor: Annotated[None, Depends(member)]) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/pause", status_code=status.HTTP_200_OK, summary="Pause project as lead"
)
async def pause_project(id: ProjectId, actor: Annotated[None, Depends(member)]) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/abandon", status_code=status.HTTP_200_OK, summary="Abandon project as lead"
)
async def abandon_project(id: ProjectId, actor: Annotated[None, Depends(member)]) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/continue",
    status_code=status.HTTP_200_OK,
    summary="Continue shipped project as lead",
)
async def continue_project(id: ProjectId, actor: Annotated[None, Depends(member)]) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/invites", status_code=status.HTTP_201_CREATED, summary="Invite a member as lead"
)
async def create_invite(
    id: ProjectId, body: CreateProjectInviteBody, actor: Annotated[None, Depends(member)]
) -> Invite:
    not_implemented()


@router.post(
    "/projects/{id}/join-requests",
    status_code=status.HTTP_201_CREATED,
    summary="Request to join a project or hackathon team",
)
async def create_join_request(
    id: ProjectId, body: CreateProjectJoinRequestBody, actor: Annotated[None, Depends(member)]
) -> JoinRequest:
    not_implemented()


@router.delete(
    "/projects/{id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a project member as lead",
)
async def remove_member(
    id: ProjectId,
    user_id: UserId,
    actor: Annotated[None, Depends(member)],
    reason: Annotated[ReasonBody | None, Body()] = None,
) -> None:
    not_implemented()


@router.post(
    "/projects/{id}/leave", status_code=status.HTTP_204_NO_CONTENT, summary="Leave a project"
)
async def leave_project(id: ProjectId, actor: Annotated[None, Depends(member)]) -> None:
    not_implemented()


@router.post(
    "/projects/{id}/claim-lead",
    status_code=status.HTTP_200_OK,
    summary="Claim lead of a paused leaderless project as member",
)
async def claim_lead(id: ProjectId, actor: Annotated[None, Depends(member)]) -> Project:
    not_implemented()


@router.get(
    "/projects/{id}/milestones", status_code=status.HTTP_200_OK, summary="List project milestones"
)
async def list_milestones(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> list[Milestone]:
    not_implemented()


@router.post(
    "/projects/{id}/milestones",
    status_code=status.HTTP_201_CREATED,
    summary="Create milestone as project member",
)
async def create_milestone(
    id: ProjectId, body: CreateMilestoneBody, actor: Annotated[None, Depends(member)]
) -> Milestone:
    not_implemented()


@router.patch(
    "/projects/{id}/milestones/{mid}",
    status_code=status.HTTP_200_OK,
    summary="Edit or complete milestone as project member",
)
async def update_milestone(
    id: ProjectId,
    mid: MilestoneId,
    body: UpdateMilestoneBody,
    actor: Annotated[None, Depends(member)],
) -> Milestone:
    not_implemented()


@router.delete(
    "/projects/{id}/milestones/{mid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete milestone as project member",
)
async def delete_milestone(
    id: ProjectId, mid: MilestoneId, actor: Annotated[None, Depends(member)]
) -> None:
    not_implemented()


@router.get(
    "/projects/{id}/updates", status_code=status.HTTP_200_OK, summary="List project updates"
)
async def list_updates(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> list[ProjectUpdate]:
    not_implemented()


@router.post(
    "/projects/{id}/updates",
    status_code=status.HTTP_201_CREATED,
    summary="Post update as project member",
)
async def create_update(
    id: ProjectId, body: CreateProjectUpdateBody, actor: Annotated[None, Depends(member)]
) -> ProjectUpdate:
    not_implemented()


@router.post(
    "/projects/{id}/releases", status_code=status.HTTP_201_CREATED, summary="Ship a release as lead"
)
async def create_release(
    id: ProjectId, body: CreateProjectReleaseBody, actor: Annotated[None, Depends(member)]
) -> ProjectRelease:
    not_implemented()


@router.get(
    "/projects/{id}/releases", status_code=status.HTTP_200_OK, summary="List project releases"
)
async def list_releases(
    id: ProjectId, actor: Annotated[None, Depends(member)]
) -> list[ProjectRelease]:
    not_implemented()
