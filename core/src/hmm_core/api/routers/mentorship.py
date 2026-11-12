from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    AreaKey,
    HelpOfferId,
    HelpRequestId,
    HelpRequestStatus,
    MentorshipId,
    MentorshipSide,
    MentorshipStatus,
    Page,
)

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.mentorship import (
    AcknowledgeMentorshipBody,
    CloseMentorshipBody,
    CreateHelpOfferBody,
    CreateHelpRequestBody,
    CreateMentorshipCheckinBody,
    HelpOffer,
    HelpRequest,
    MentorOffers,
    Mentorship,
    MentorshipCheckin,
    ReplaceMentorOffersBody,
    UpdateHelpRequestBody,
)
from hmm_core.api.security import MemberAuth

router = APIRouter(tags=["mentorship"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["mentorship"], responses=PROBLEM_RESPONSES)


@router.get("/me/mentor-offers", status_code=status.HTTP_200_OK, summary="List own mentor offers")
async def get_mentor_offers(actor: MemberAuth) -> MentorOffers:
    not_implemented()


@router.put(
    "/me/mentor-offers",
    status_code=status.HTTP_200_OK,
    summary="Replace own mentor offers and capacity",
)
async def replace_mentor_offers(body: ReplaceMentorOffersBody, actor: MemberAuth) -> MentorOffers:
    not_implemented()


@router.get("/help-requests", status_code=status.HTTP_200_OK, summary="List help requests")
async def list_help_requests(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    area: Annotated[AreaKey | None, Query()] = None,
    request_status: Annotated[HelpRequestStatus, Query(alias="status")] = HelpRequestStatus.OPEN,
) -> Page[HelpRequest]:
    not_implemented()


@router.post("/help-requests", status_code=status.HTTP_201_CREATED, summary="Create a help request")
async def create_help_request(body: CreateHelpRequestBody, actor: MemberAuth) -> HelpRequest:
    not_implemented()


@router.get(
    "/help-requests/{id}",
    status_code=status.HTTP_200_OK,
    summary="Get help request respecting direct-request visibility",
)
async def get_help_request(id: HelpRequestId, actor: MemberAuth) -> HelpRequest:
    not_implemented()


@router.patch(
    "/help-requests/{id}",
    status_code=status.HTTP_200_OK,
    summary="Edit open help request as learner",
)
async def update_help_request(
    id: HelpRequestId, body: UpdateHelpRequestBody, actor: MemberAuth
) -> HelpRequest:
    not_implemented()


@router.post(
    "/help-requests/{id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel help request as learner",
)
async def cancel_help_request(id: HelpRequestId, actor: MemberAuth) -> HelpRequest:
    not_implemented()


@router.post(
    "/help-requests/{id}/decline",
    status_code=status.HTTP_200_OK,
    summary="Decline an open direct help request as its target mentor",
    description="Only the target mentor may decline a direct request in the open state. "
    "Notify the learner, who may immediately open it to everyone without the three-day wait.",
)
async def decline_help_request(id: HelpRequestId, actor: MemberAuth) -> HelpRequest:
    not_implemented()


@router.post(
    "/help-requests/{id}/open",
    status_code=status.HTTP_200_OK,
    summary="Open direct help request to everyone as learner",
)
async def open_help_request(id: HelpRequestId, actor: MemberAuth) -> HelpRequest:
    not_implemented()


@router.post(
    "/help-requests/{id}/offers",
    status_code=status.HTTP_201_CREATED,
    summary="Offer help to learner",
)
async def create_help_offer(
    id: HelpRequestId, body: CreateHelpOfferBody, actor: MemberAuth
) -> HelpOffer:
    not_implemented()


@router.delete(
    "/help-offers/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Withdraw own help offer"
)
async def withdraw_help_offer(id: HelpOfferId, actor: MemberAuth) -> None:
    not_implemented()


@router.post(
    "/help-offers/{id}/accept",
    status_code=status.HTTP_201_CREATED,
    summary="Accept help offer and start mentorship as learner",
)
async def accept_help_offer(id: HelpOfferId, actor: MemberAuth) -> Mentorship:
    not_implemented()


@router.get("/me/mentorships", status_code=status.HTTP_200_OK, summary="List own mentorships")
async def list_my_mentorships(
    actor: MemberAuth,
    role: Annotated[MentorshipSide | None, Query()] = None,
    mentorship_status: Annotated[MentorshipStatus | None, Query(alias="status")] = None,
) -> list[Mentorship]:
    not_implemented()


@router.get(
    "/mentorships/{id}",
    status_code=status.HTTP_200_OK,
    summary="Get mentorship as participant or admin",
)
async def get_mentorship(id: MentorshipId, actor: MemberAuth) -> Mentorship:
    not_implemented()


@router.post(
    "/mentorships/{id}/checkins",
    status_code=status.HTTP_201_CREATED,
    summary="Record check-in as either participant",
)
async def create_checkin(
    id: MentorshipId, body: CreateMentorshipCheckinBody, actor: MemberAuth
) -> MentorshipCheckin:
    not_implemented()


@router.post(
    "/mentorships/{id}/close", status_code=status.HTTP_200_OK, summary="Close mentorship as learner"
)
async def close_mentorship(
    id: MentorshipId, body: CloseMentorshipBody, actor: MemberAuth
) -> Mentorship:
    not_implemented()


@router.post(
    "/mentorships/{id}/propose-close",
    status_code=status.HTTP_200_OK,
    summary="Propose closing as mentor",
)
async def propose_close(id: MentorshipId, actor: MemberAuth) -> Mentorship:
    not_implemented()


@router.post(
    "/mentorships/{id}/acknowledge",
    status_code=status.HTTP_200_OK,
    summary="Acknowledge learner outcome as mentor",
)
async def acknowledge_outcome(
    id: MentorshipId, body: AcknowledgeMentorshipBody, actor: MemberAuth
) -> Mentorship:
    not_implemented()


@router.post(
    "/mentorships/{id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel mentorship before first check-in as either participant",
)
async def cancel_mentorship(id: MentorshipId, actor: MemberAuth) -> Mentorship:
    not_implemented()
