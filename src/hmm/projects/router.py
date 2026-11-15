from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status

from hmm import models
from hmm.common import (
    AreaKey,
    EventId,
    MilestoneId,
    ProjectId,
    ProjectStatus,
    TeamStatus,
    UserId,
)
from hmm.formation import invites as formation_invites
from hmm.formation import schemas as formation_schemas
from hmm.formation import teams as formation_teams
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES, ProblemError, problem_response
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import ReasonBody
from hmm.platform.security import MemberAuth
from hmm.projects import invites, join_requests, lifecycle, records
from hmm.projects import members as projects_members
from hmm.projects import milestones as projects_milestones
from hmm.projects import releases as projects_releases
from hmm.projects import updates as projects_updates
from hmm.projects.schemas import (
    CreateMilestoneBody,
    CreateProjectBody,
    CreateProjectInviteBody,
    CreateProjectJoinRequestBody,
    CreateProjectReleaseBody,
    CreateProjectUpdateBody,
    JoinRequest,
    Milestone,
    Project,
    ProjectListViewPage,
    ProjectRelease,
    ProjectSummary,
    ProjectUpdate,
    ProjectView,
    UpdateMilestoneBody,
    UpdateProjectBody,
)
from hmm.projects.views import POST_RESULT_TEAM_STATUSES
from hmm.recommendations import service as recommendations

router = APIRouter(tags=["projects"], responses=PROBLEM_RESPONSES)


@router.get("/projects", status_code=status.HTTP_200_OK, summary="List projects")
async def list_projects(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    recruiting: Annotated[bool | None, Query()] = None,
    event: Annotated[EventId | None, Query()] = None,
    member_id: Annotated[UserId | None, Query(alias="member")] = None,
) -> ProjectListViewPage:
    return await records.list_projects(
        session,
        actor,
        pagination,
        status=project_status,
        area=area,
        recruiting=recruiting,
        event_id=event,
        member_id=member_id,
        now=clock.now(),
    )


@router.post(
    "/projects", status_code=status.HTTP_201_CREATED, summary="Create a standalone project"
)
async def create_project(
    body: CreateProjectBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Project:
    return await records.create_project(session, request.app.state.jobs, body, actor, clock.now())


@router.get(
    "/me/projects", status_code=status.HTTP_200_OK, summary="List own projects and hackathon teams"
)
async def list_my_projects(
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    project_status: Annotated[ProjectStatus | TeamStatus | None, Query(alias="status")] = None,
) -> list[ProjectSummary]:
    return await records.list_my_projects(session, actor, project_status, clock.now())


@router.get(
    "/projects/{id}", status_code=status.HTTP_200_OK, summary="Get project respecting visibility"
)
async def get_project(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> ProjectView:
    return await records.get_project(session, id, actor, clock.now())


@router.patch("/projects/{id}", status_code=status.HTTP_200_OK, summary="Edit project as lead")
async def update_project(
    id: ProjectId,
    body: UpdateProjectBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Project:
    return await lifecycle.update_project(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )


@router.post(
    "/projects/{id}/start", status_code=status.HTTP_200_OK, summary="Start project as lead"
)
async def start_project(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Project:
    return await lifecycle.start_project(session, id, actor, clock.now())


@router.post(
    "/projects/{id}/pause", status_code=status.HTTP_200_OK, summary="Pause project as lead"
)
async def pause_project(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Project:
    return await lifecycle.pause_project(session, id, actor, clock.now())


@router.post(
    "/projects/{id}/abandon", status_code=status.HTTP_200_OK, summary="Abandon project as lead"
)
async def abandon_project(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Project:
    return await lifecycle.abandon_project(session, id, actor, clock.now())


@router.post(
    "/projects/{id}/continue",
    status_code=status.HTTP_200_OK,
    summary="Continue shipped project as lead",
)
async def continue_project(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Project:
    return await lifecycle.continue_project(session, id, actor, clock.now())


@router.post(
    "/projects/{id}/invites", status_code=status.HTTP_201_CREATED, summary="Invite a member as lead"
)
async def create_invite(
    id: ProjectId,
    body: CreateProjectInviteBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> formation_schemas.Invite:
    project = await session.get(models.Project, id)
    if (
        project is not None
        and project.team_status is not None
        and project.team_status not in POST_RESULT_TEAM_STATUSES
    ):
        return await formation_invites.create_invite(
            session, id, formation_schemas.InviteBody(user_id=body.user_id), actor, clock.now()
        )
    invite = await invites.create_invite(session, id, body, actor, clock.now())
    await recommendations.enqueue_project_refresh(session, request.app.state.jobs, id)
    return invite


@router.post(
    "/projects/{id}/join-requests",
    status_code=status.HTTP_201_CREATED,
    summary="Request to join a standalone project",
)
async def create_join_request(
    id: ProjectId,
    body: CreateProjectJoinRequestBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> JoinRequest:
    join_request = await join_requests.create_join_request(session, id, body, actor, clock.now())
    await recommendations.enqueue_project_refresh(session, request.app.state.jobs, id)
    return join_request


@router.delete(
    "/projects/{id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a project member as lead",
)
async def remove_member(
    id: ProjectId,
    user_id: UserId,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
    reason: Annotated[ReasonBody | None, Body()] = None,
) -> None:
    await projects_members.remove_project_member(
        session, request.app.state.jobs, id, user_id, reason, actor, clock.now()
    )


@router.post(
    "/projects/{id}/leave", status_code=status.HTTP_204_NO_CONTENT, summary="Leave a project"
)
async def leave_project(
    id: ProjectId,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
    body: formation_schemas.DeclinePlacementBody | None = None,
) -> None:
    await projects_members.leave_project(
        session, request.app.state.jobs, id, actor, clock.now(), body
    )


@router.post(
    "/projects/{id}/transfer-lead",
    status_code=status.HTTP_200_OK,
    summary="Transfer project lead immediately",
)
async def transfer_lead(
    id: ProjectId,
    body: formation_schemas.TransferLeadBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Project | formation_schemas.Team:
    project = await session.get(models.Project, id)
    if (
        project is not None
        and project.team_status is not None
        and project.team_status not in POST_RESULT_TEAM_STATUSES
    ):
        return await formation_teams.transfer_lead(session, id, body, actor, clock.now())
    return await projects_members.transfer_project_lead(
        session, request.app.state.jobs, id, body.user_id, actor, clock.now()
    )


@router.post("/invites/{id}/accept")
async def accept_invite(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> formation_schemas.Invite:
    row = await session.get(models.Invite, id)
    if row is not None and row.event_id is None:
        result = await invites.respond_invite(session, id, actor, clock.now(), "accepted")
    else:
        result = await formation_invites.accept_invite(session, id, actor, clock.now())
    if isinstance(result, ProblemError):
        return cast(formation_schemas.Invite, problem_response(request, result.code, result.detail))
    return result


@router.post("/invites/{id}/decline")
async def decline_invite(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> formation_schemas.Invite:
    row = await session.get(models.Invite, id)
    if row is not None and row.event_id is None:
        result = await invites.respond_invite(session, id, actor, clock.now(), "declined")
    else:
        result = await formation_invites.decline_invite(session, id, actor, clock.now())
    if isinstance(result, ProblemError):
        return cast(formation_schemas.Invite, problem_response(request, result.code, result.detail))
    return result


@router.delete("/invites/{id}", status_code=204)
async def withdraw_invite(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> Response:
    row = await session.get(models.Invite, id)
    if row is not None and row.event_id is None:
        result = await invites.respond_invite(session, id, actor, clock.now(), "withdrawn")
    else:
        result = await formation_invites.withdraw_invite(session, id, actor, clock.now())
    if isinstance(result, ProblemError):
        return problem_response(request, result.code, result.detail)
    return Response(status_code=204)


@router.get("/projects/{id}/join-requests")
async def list_join_requests(
    id: ProjectId, actor: MemberAuth, session: Session
) -> list[JoinRequest]:
    return await join_requests.list_join_requests(session, id, actor)


@router.post("/join-requests/{id}/accept")
async def accept_join_request(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> JoinRequest:
    return await join_requests.respond_join_request(
        session, request.app.state.jobs, id, actor, clock.now(), "accepted"
    )


@router.post("/join-requests/{id}/reject")
async def reject_join_request(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> JoinRequest:
    return await join_requests.respond_join_request(
        session, request.app.state.jobs, id, actor, clock.now(), "rejected"
    )


@router.delete("/join-requests/{id}", status_code=204)
async def withdraw_join_request(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> Response:
    await join_requests.respond_join_request(
        session, request.app.state.jobs, id, actor, clock.now(), "withdrawn"
    )
    return Response(status_code=204)


@router.get(
    "/projects/{id}/milestones", status_code=status.HTTP_200_OK, summary="List project milestones"
)
async def list_milestones(id: ProjectId, actor: MemberAuth, session: Session) -> list[Milestone]:
    return await projects_milestones.list_milestones(session, id, actor)


@router.post(
    "/projects/{id}/milestones",
    status_code=status.HTTP_201_CREATED,
    summary="Create milestone as project member",
)
async def create_milestone(
    id: ProjectId,
    body: CreateMilestoneBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> Milestone:
    return await projects_milestones.create_milestone(session, id, body, actor, clock.now())


@router.patch(
    "/projects/{id}/milestones/{mid}",
    status_code=status.HTTP_200_OK,
    summary="Edit or complete milestone as project member",
)
async def update_milestone(
    id: ProjectId,
    mid: MilestoneId,
    body: UpdateMilestoneBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Milestone:
    return await projects_milestones.update_milestone(
        session, request.app.state.jobs, id, mid, body, actor, clock.now()
    )


@router.delete(
    "/projects/{id}/milestones/{mid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete milestone as project member",
)
async def delete_milestone(
    id: ProjectId,
    mid: MilestoneId,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> None:
    await projects_milestones.delete_milestone(
        session, request.app.state.jobs, id, mid, actor, clock.now()
    )


@router.get(
    "/projects/{id}/updates", status_code=status.HTTP_200_OK, summary="List project updates"
)
async def list_updates(id: ProjectId, actor: MemberAuth, session: Session) -> list[ProjectUpdate]:
    return await projects_updates.list_updates(session, id, actor)


@router.post(
    "/projects/{id}/updates",
    status_code=status.HTTP_201_CREATED,
    summary="Post update as project member",
)
async def create_update(
    id: ProjectId,
    body: CreateProjectUpdateBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> ProjectUpdate:
    return await projects_updates.create_update(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )


@router.post(
    "/projects/{id}/releases", status_code=status.HTTP_201_CREATED, summary="Ship a release as lead"
)
async def create_release(
    id: ProjectId,
    body: CreateProjectReleaseBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> ProjectRelease:
    return await projects_releases.create_release(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )


@router.get(
    "/projects/{id}/releases", status_code=status.HTTP_200_OK, summary="List project releases"
)
async def list_releases(id: ProjectId, actor: MemberAuth, session: Session) -> list[ProjectRelease]:
    return await projects_releases.list_releases(session, id, actor)
