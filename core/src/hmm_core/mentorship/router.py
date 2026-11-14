from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from hmm_contracts.common import (
    AreaKey,
    HelpOfferId,
    HelpRequestId,
    HelpRequestStatus,
    MentorshipId,
    MentorshipSide,
    MentorshipStatus,
)

from hmm_core.mentorship import service
from hmm_core.mentorship.schemas import (
    AcknowledgeMentorshipBody,
    CloseMentorshipBody,
    CreateHelpOfferBody,
    CreateHelpRequestBody,
    CreateMentorshipCheckinBody,
    HelpOffer,
    HelpRequest,
    HelpRequestPage,
    MentorOffers,
    Mentorship,
    MentorshipCheckin,
    ReplaceMentorOffersBody,
    UpdateHelpRequestBody,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import MemberAuth

router = APIRouter(tags=["mentorship"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["mentorship"], responses=PROBLEM_RESPONSES)


@router.get("/me/mentor-offers", status_code=status.HTTP_200_OK, summary="List own mentor offers")
async def get_mentor_offers(
    actor: MemberAuth, session: Session, clock: CurrentClock
) -> MentorOffers:
    return await service.mentor_offers_view(session, actor, clock.now())


@router.put(
    "/me/mentor-offers",
    status_code=status.HTTP_200_OK,
    summary="Replace own mentor offers and capacity",
)
async def replace_mentor_offers(
    body: ReplaceMentorOffersBody, actor: MemberAuth, session: Session, clock: CurrentClock
) -> MentorOffers:
    return await service.replace_mentor_offers(session, actor, body, clock.now())


@router.get("/help-requests", status_code=status.HTTP_200_OK, summary="List help requests")
async def list_help_requests(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    area: Annotated[AreaKey | None, Query()] = None,
    request_status: Annotated[HelpRequestStatus, Query(alias="status")] = HelpRequestStatus.OPEN,
) -> HelpRequestPage:
    return await service.list_help_requests(
        session,
        actor,
        pagination,
        area_key=area,
        request_status=request_status,
        now=clock.now(),
    )


@router.post("/help-requests", status_code=status.HTTP_201_CREATED, summary="Create a help request")
async def create_help_request(
    body: CreateHelpRequestBody,
    actor: MemberAuth,
    request: Request,
    session: Session,
    clock: CurrentClock,
) -> HelpRequest:
    return await service.create_help_request(
        session, request.app.state.jobs, actor, body, clock.now()
    )


@router.get(
    "/help-requests/{id}",
    status_code=status.HTTP_200_OK,
    summary="Get help request respecting direct-request visibility",
)
async def get_help_request(
    id: HelpRequestId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> HelpRequest:
    return await service.get_help_request(session, actor, id, clock.now())


@router.patch(
    "/help-requests/{id}",
    status_code=status.HTTP_200_OK,
    summary="Edit open help request as learner",
)
async def update_help_request(
    id: HelpRequestId,
    body: UpdateHelpRequestBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> HelpRequest:
    return await service.update_help_request(session, actor, id, body, clock.now())


@router.post(
    "/help-requests/{id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel help request as learner",
)
async def cancel_help_request(
    id: HelpRequestId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> HelpRequest:
    return await service.cancel_help_request(session, actor, id, clock.now())


@router.post(
    "/help-requests/{id}/decline",
    status_code=status.HTTP_200_OK,
    summary="Decline an open direct help request as its target mentor",
    description="Only the target mentor may decline a direct request in the open state. "
    "Notify the learner, who may immediately open it to everyone without the three-day wait.",
)
async def decline_help_request(
    id: HelpRequestId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> HelpRequest:
    return await service.decline_help_request(
        session, request.app.state.jobs, actor, id, clock.now()
    )


@router.post(
    "/help-requests/{id}/open",
    status_code=status.HTTP_200_OK,
    summary="Open direct help request to everyone as learner",
)
async def open_help_request(
    id: HelpRequestId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> HelpRequest:
    return await service.open_help_request(session, request.app.state.jobs, actor, id, clock.now())


@router.post(
    "/help-requests/{id}/offers",
    status_code=status.HTTP_201_CREATED,
    summary="Offer help to learner",
)
async def create_help_offer(
    id: HelpRequestId,
    body: CreateHelpOfferBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> HelpOffer:
    return await service.create_help_offer(session, actor, id, body, clock.now())


@router.delete(
    "/help-offers/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw own help offer"
)
async def withdraw_help_offer(
    id: HelpOfferId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> None:
    await service.withdraw_help_offer(session, actor, id, clock.now())


@router.post(
    "/help-offers/{id}/accept",
    status_code=status.HTTP_201_CREATED,
    summary="Accept help offer and start mentorship as learner",
)
async def accept_help_offer(
    id: HelpOfferId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Mentorship:
    return await service.accept_help_offer(session, actor, id, clock.now())


@router.get("/me/mentorships", status_code=status.HTTP_200_OK, summary="List own mentorships")
async def list_my_mentorships(
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    role: Annotated[MentorshipSide | None, Query()] = None,
    mentorship_status: Annotated[MentorshipStatus | None, Query(alias="status")] = None,
) -> list[Mentorship]:
    return await service.list_my_mentorships(session, actor, role, mentorship_status, clock.now())


@router.get(
    "/mentorships/{id}",
    status_code=status.HTTP_200_OK,
    summary="Get mentorship as participant or admin",
)
async def get_mentorship(
    id: MentorshipId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Mentorship:
    return await service.get_mentorship(session, actor, id, clock.now())


@router.post(
    "/mentorships/{id}/checkins",
    status_code=status.HTTP_201_CREATED,
    summary="Record check-in as either participant",
)
async def create_checkin(
    id: MentorshipId,
    body: CreateMentorshipCheckinBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> MentorshipCheckin:
    return await service.create_checkin(
        session, request.app.state.jobs, actor, id, body, clock.now()
    )


@router.post(
    "/mentorships/{id}/close", status_code=status.HTTP_200_OK, summary="Close mentorship as learner"
)
async def close_mentorship(
    id: MentorshipId,
    body: CloseMentorshipBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> Mentorship:
    return await service.close_mentorship(
        session, request.app.state.jobs, actor, id, body, clock.now()
    )


@router.post(
    "/mentorships/{id}/propose-close",
    status_code=status.HTTP_200_OK,
    summary="Propose closing as mentor",
)
async def propose_close(
    id: MentorshipId, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> Mentorship:
    return await service.propose_close(session, request.app.state.jobs, actor, id, clock.now())


@router.post(
    "/mentorships/{id}/acknowledge",
    status_code=status.HTTP_200_OK,
    summary="Acknowledge learner outcome as mentor",
)
async def acknowledge_outcome(
    id: MentorshipId,
    body: AcknowledgeMentorshipBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> Mentorship:
    return await service.acknowledge_outcome(
        session, request.app.state.jobs, actor, id, body, clock.now()
    )


@router.post(
    "/mentorships/{id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel mentorship before first check-in as either participant",
)
async def cancel_mentorship(
    id: MentorshipId, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> Mentorship:
    return await service.cancel_mentorship(session, request.app.state.jobs, actor, id, clock.now())
