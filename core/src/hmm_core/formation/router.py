from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status
from hmm_contracts.common import (
    EnrollmentStatus,
    ErrorCode,
    EventId,
    InviteId,
    InviteStatus,
    JoinRequestId,
    LeadOfferId,
    LeadOfferStatus,
    MergeProposalId,
    ProjectId,
    SwapRequestId,
    UserId,
)

from hmm_core.formation import service
from hmm_core.formation.schemas import (
    AddMemberBody,
    AdminAddMemberBody,
    AdminCreateTeamBody,
    AssignLeadBody,
    CreateEnrollmentBody,
    CreateMergeBody,
    CreateSwapBody,
    CreateTeamBody,
    DeclinePlacementBody,
    Enrollment,
    EnrollmentPage,
    Formation,
    FormationRun,
    LeadCandidate,
    LeadEligibility,
    Merge,
    MoveMemberBody,
    Swap,
    TransferLeadBody,
    UpdateEnrollmentBody,
    UpdateFormationBody,
    UpdateMemberBody,
    UpdateReserveOrderBody,
    UpdateTeamStatusBody,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import (
    PROBLEM_RESPONSES,
    ProblemError,
    not_implemented,
    problem_response,
)
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.security import AdminAuth, MemberAuth
from hmm_core.projects.schemas import Invite, JoinRequest, LeadOffer, Project, ProjectMember

router = APIRouter(tags=["formation"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["formation"], responses=PROBLEM_RESPONSES)


@router.get(
    "/events/{id}/formation", status_code=status.HTTP_200_OK, summary="Get participation state"
)
async def get_formation(
    id: EventId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Formation:
    event = await service.formation_event(session, id, actor)
    formation = await service.formation_participation(session, event, actor, clock.now())
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return formation


@router.post(
    "/events/{id}/enroll", status_code=status.HTTP_201_CREATED, summary="Enroll in an event"
)
async def enroll(
    id: EventId,
    body: CreateEnrollmentBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
    response: Response,
) -> Enrollment:
    enrollment, changed = await service.enroll(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )
    if not changed:
        response.status_code = status.HTTP_200_OK
    return (await service.enrollment_views(session, [enrollment], actor, clock.now()))[0]


@router.patch("/events/{id}/enroll", status_code=status.HTTP_200_OK, summary="Edit enrollment")
async def update_enrollment(
    id: EventId,
    body: UpdateEnrollmentBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> Enrollment:
    row = await service.update_enrollment(session, id, body, actor, clock.now())
    return (await service.enrollment_views(session, [row], actor, clock.now()))[0]


@router.delete(
    "/events/{id}/enroll", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw enrollment"
)
async def withdraw_enrollment(
    id: EventId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> Response:
    await service.withdraw_enrollment(session, request.app.state.jobs, id, actor, clock.now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/events/{id}/enrollments", status_code=status.HTTP_200_OK, summary="List enrolled members"
)
async def list_enrollments(
    id: EventId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
) -> EnrollmentPage:
    return await service.list_enrollments(session, id, actor, pagination, clock.now())


@router.get("/events/{id}/reserve", status_code=status.HTTP_200_OK, summary="List ranked reserve")
async def list_reserve(id: EventId, actor: MemberAuth) -> list[Enrollment]:
    not_implemented()


@router.get("/me/enrollments", status_code=status.HTTP_200_OK, summary="List my enrollments")
async def list_my_enrollments(
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    status: Annotated[EnrollmentStatus | None, Query()] = None,
) -> list[Enrollment]:
    return await service.list_my_enrollments(session, actor, status, clock.now())


@router.get("/events/{id}/lead-fit/me", status_code=status.HTTP_200_OK, summary="Get my lead fit")
async def get_my_lead_fit(id: EventId, actor: MemberAuth, session: Session) -> LeadEligibility:
    return await service.my_lead_fit(session, id, actor)


@router.post(
    "/events/{id}/projects", status_code=status.HTTP_201_CREATED, summary="Claim a team lead role"
)
async def claim_lead(
    id: EventId,
    body: CreateTeamBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Project:
    project = await service.claim_lead(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )
    if project is None:
        return cast(
            Project,
            problem_response(request, ErrorCode.LEAD_FIT_PENDING, "Lead fit is being computed"),
        )
    return await service.team_view(session, project, actor, clock.now())


@router.post("/projects/{id}/ready", status_code=status.HTTP_200_OK, summary="Mark a core ready")
async def mark_ready(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Project:
    project = await service.set_ready(session, id, actor, clock.now(), ready=True)
    return await service.team_view(session, project, actor, clock.now())


@router.delete(
    "/projects/{id}/ready", status_code=status.HTTP_204_NO_CONTENT, summary="Clear core readiness"
)
async def clear_ready(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await service.set_ready(session, id, actor, clock.now(), ready=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/projects/{id}/unclaim", status_code=status.HTTP_200_OK, summary="Unclaim and disband a core"
)
async def unclaim_lead(
    id: ProjectId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> Project:
    project = await service.unclaim(session, request.app.state.jobs, id, actor, clock.now())
    return await service.team_view(session, project, actor, clock.now())


@router.post(
    "/projects/{id}/seats/close", status_code=status.HTTP_200_OK, summary="Close open team seats"
)
async def close_seats(id: ProjectId, actor: MemberAuth) -> Project:
    not_implemented()


@router.delete(
    "/projects/{id}/seats/close",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reopen team seats",
)
async def reopen_seats(id: ProjectId, actor: MemberAuth) -> Response:
    not_implemented()


@router.get(
    "/projects/{id}/join-requests",
    status_code=status.HTTP_200_OK,
    summary="List team join requests",
)
async def list_join_requests(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[JoinRequest]:
    return await service.list_join_requests(session, id, actor)


@router.delete(
    "/join-requests/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw a join request"
)
async def withdraw_join_request(
    id: JoinRequestId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await service.withdraw_join_request(session, id, actor, clock.now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/join-requests/{id}/accept", status_code=status.HTTP_200_OK, summary="Accept a join request"
)
async def accept_join_request(
    id: JoinRequestId,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> JoinRequest:
    row = await service.accept_join_request(session, request.app.state.jobs, id, actor, clock.now())
    return (await service.join_request_views(session, [row], actor))[0]


@router.post(
    "/join-requests/{id}/reject", status_code=status.HTTP_200_OK, summary="Reject a join request"
)
async def reject_join_request(
    id: JoinRequestId,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> JoinRequest:
    row = await service.reject_join_request(session, id, actor, clock.now())
    return (await service.join_request_views(session, [row], actor))[0]


@router.get("/projects/{id}/invites", status_code=status.HTTP_200_OK, summary="List team invites")
async def list_invites(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[Invite]:
    return await service.list_invites(session, id, actor)


@router.delete(
    "/invites/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw an invite"
)
async def withdraw_invite(
    id: InviteId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await service.withdraw_invite(session, id, actor, clock.now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/invites", status_code=status.HTTP_200_OK, summary="List my invites")
async def list_my_invites(
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    status: Annotated[InviteStatus | None, Query()] = None,
) -> list[Invite]:
    return await service.list_my_invites(session, status, actor)


@router.post("/invites/{id}/accept", status_code=status.HTTP_200_OK, summary="Accept an invite")
async def accept_invite(
    id: InviteId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> Invite:
    invite = await service.accept_invite(session, request.app.state.jobs, id, actor, clock.now())
    return (await service.invite_views(session, [invite], actor))[0]


@router.post("/invites/{id}/decline", status_code=status.HTTP_200_OK, summary="Decline an invite")
async def decline_invite(
    id: InviteId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Invite:
    invite = await service.decline_invite(session, id, actor, clock.now())
    return (await service.invite_views(session, [invite], actor))[0]


@router.post(
    "/projects/{id}/placement/confirm",
    status_code=status.HTTP_200_OK,
    summary="Confirm placement or presence",
)
async def confirm_placement(id: ProjectId, actor: MemberAuth) -> ProjectMember:
    not_implemented()


@router.post(
    "/projects/{id}/placement/decline",
    status_code=status.HTTP_200_OK,
    summary="Decline placement or presence",
)
async def decline_placement(
    id: ProjectId, body: DeclinePlacementBody, actor: MemberAuth
) -> Enrollment:
    not_implemented()


@router.get("/me/lead-offers", status_code=status.HTTP_200_OK, summary="List my lead offers")
async def list_my_lead_offers(
    actor: MemberAuth,
    status: Annotated[LeadOfferStatus | None, Query()] = None,
) -> list[LeadOffer]:
    not_implemented()


@router.post(
    "/lead-offers/{id}/accept", status_code=status.HTTP_200_OK, summary="Accept a lead role"
)
async def accept_lead_offer(id: LeadOfferId, actor: MemberAuth) -> LeadOffer:
    not_implemented()


@router.post(
    "/lead-offers/{id}/decline", status_code=status.HTTP_200_OK, summary="Decline a lead role"
)
async def decline_lead_offer(id: LeadOfferId, actor: MemberAuth) -> LeadOffer:
    not_implemented()


@router.delete(
    "/lead-offers/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a lead transfer offer",
)
async def cancel_lead_offer(id: LeadOfferId, actor: MemberAuth) -> Response:
    not_implemented()


@router.post(
    "/projects/{id}/transfer-lead",
    status_code=status.HTTP_201_CREATED,
    summary="Offer lead transfer",
)
async def transfer_lead(id: ProjectId, body: TransferLeadBody, actor: MemberAuth) -> LeadOffer:
    not_implemented()


@router.post(
    "/projects/{id}/lead/step-down", status_code=status.HTTP_200_OK, summary="Step down as lead"
)
async def step_down(id: ProjectId, actor: MemberAuth) -> Project:
    not_implemented()


@router.post(
    "/projects/{id}/members",
    status_code=status.HTTP_201_CREATED,
    summary="Propose a reserve member",
)
async def add_reserve_member(
    id: ProjectId, body: AddMemberBody, actor: MemberAuth
) -> ProjectMember:
    not_implemented()


@router.patch(
    "/projects/{id}/members/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Assign a member area",
)
async def update_member_area(
    id: ProjectId, user_id: UserId, body: UpdateMemberBody, actor: MemberAuth
) -> ProjectMember:
    not_implemented()


@router.post(
    "/projects/{id}/swaps", status_code=status.HTTP_201_CREATED, summary="Propose a member swap"
)
async def create_swap(id: ProjectId, body: CreateSwapBody, actor: MemberAuth) -> Swap:
    not_implemented()


@router.post("/swaps/{id}/accept", status_code=status.HTTP_200_OK, summary="Accept a member swap")
async def accept_swap(id: SwapRequestId, actor: MemberAuth) -> Swap:
    not_implemented()


@router.post("/swaps/{id}/decline", status_code=status.HTTP_200_OK, summary="Decline a member swap")
async def decline_swap(id: SwapRequestId, actor: MemberAuth) -> Swap:
    not_implemented()


@router.post(
    "/projects/{id}/merges", status_code=status.HTTP_201_CREATED, summary="Propose a team merge"
)
async def create_merge(id: ProjectId, body: CreateMergeBody, actor: MemberAuth) -> Merge:
    not_implemented()


@router.post("/merges/{id}/accept", status_code=status.HTTP_200_OK, summary="Accept a team merge")
async def accept_merge(id: MergeProposalId, actor: MemberAuth) -> Merge:
    not_implemented()


@router.post("/merges/{id}/decline", status_code=status.HTTP_200_OK, summary="Decline a team merge")
async def decline_merge(id: MergeProposalId, actor: MemberAuth) -> Merge:
    not_implemented()


@router.patch(
    "/admin/events/{id}/formation",
    status_code=status.HTTP_200_OK,
    summary="Override formation settings",
)
async def update_formation(id: EventId, body: UpdateFormationBody, actor: AdminAuth) -> Formation:
    not_implemented()


@router.post(
    "/admin/events/{id}/formation/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue formation",
)
async def run_formation(
    id: EventId, actor: AdminAuth, body: ReasonBody | None = None
) -> FormationRun:
    not_implemented()


@router.get(
    "/admin/events/{id}/formation/runs",
    status_code=status.HTTP_200_OK,
    summary="Inspect formation runs",
)
async def list_formation_runs(id: EventId, actor: AdminAuth) -> list[FormationRun]:
    not_implemented()


@router.get(
    "/admin/events/{id}/lead-candidates",
    status_code=status.HTTP_200_OK,
    summary="List lead candidates",
)
async def list_lead_candidates(
    id: EventId, actor: AdminAuth, session: Session
) -> list[LeadCandidate]:
    return await service.list_lead_candidates(session, id)


@router.post(
    "/admin/events/{id}/projects",
    status_code=status.HTTP_201_CREATED,
    summary="Create a team with an assigned lead",
)
async def admin_create_team(id: EventId, body: AdminCreateTeamBody, actor: AdminAuth) -> Project:
    not_implemented()


@router.put(
    "/admin/events/{id}/reserve/order",
    status_code=status.HTTP_200_OK,
    summary="Reorder the reserve",
)
async def update_reserve_order(
    id: EventId, body: UpdateReserveOrderBody, actor: AdminAuth
) -> list[Enrollment]:
    not_implemented()


@router.post(
    "/admin/projects/{id}/members", status_code=status.HTTP_201_CREATED, summary="Add a team member"
)
async def admin_add_member(
    id: ProjectId, body: AdminAddMemberBody, actor: AdminAuth
) -> ProjectMember:
    not_implemented()


@router.delete(
    "/admin/projects/{id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a team member",
)
async def admin_remove_member(
    id: ProjectId,
    user_id: UserId,
    actor: AdminAuth,
    reason: Annotated[str | None, Query()] = None,
) -> Response:
    not_implemented()


@router.post(
    "/admin/projects/{id}/move",
    status_code=status.HTTP_200_OK,
    summary="Move a member to another team",
)
async def admin_move_member(id: ProjectId, body: MoveMemberBody, actor: AdminAuth) -> ProjectMember:
    not_implemented()


@router.put(
    "/admin/projects/{id}/lead", status_code=status.HTTP_200_OK, summary="Assign the team lead"
)
async def admin_assign_lead(id: ProjectId, body: AssignLeadBody, actor: AdminAuth) -> Project:
    not_implemented()


@router.post(
    "/admin/projects/{id}/disband", status_code=status.HTTP_200_OK, summary="Disband a team"
)
async def admin_disband_team(id: ProjectId, body: ReasonBody, actor: AdminAuth) -> Project:
    not_implemented()


@router.post(
    "/admin/projects/{id}/status", status_code=status.HTTP_200_OK, summary="Override team status"
)
async def admin_update_team_status(
    id: ProjectId, body: UpdateTeamStatusBody, actor: AdminAuth
) -> Project:
    not_implemented()
