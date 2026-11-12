from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    EventId,
    Page,
    PlaceStatus,
    ProjectId,
    ReviewContext,
    ReviewId,
    ReviewWindowId,
    UserId,
)

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.results import (
    AdminReview,
    CreateResultBody,
    PendingReviewWindow,
    ProjectResult,
    RemoveReviewBody,
    ReportReviewBody,
    ResultToVerify,
    Review,
    ReviewReport,
    UpdateHostReviewBody,
    UpdateResultBody,
    UpdateReviewBody,
    UserReviews,
    VerifyResultBody,
)
from hmm_core.api.security import AdminAuth, MemberAuth

router = APIRouter(tags=["results"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["results"], responses=PROBLEM_RESPONSES)


@router.post(
    "/projects/{id}/result", status_code=status.HTTP_201_CREATED, summary="Submit hackathon result"
)
async def create_result(id: ProjectId, body: CreateResultBody, actor: MemberAuth) -> ProjectResult:
    not_implemented()


@router.patch(
    "/projects/{id}/result",
    status_code=status.HTTP_200_OK,
    summary="Edit result within the review window",
)
async def update_result(id: ProjectId, body: UpdateResultBody, actor: MemberAuth) -> ProjectResult:
    not_implemented()


@router.get(
    "/admin/results",
    status_code=status.HTTP_200_OK,
    summary="List results to verify with parser winner candidates",
)
async def list_results_to_verify(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    place_status: Annotated[PlaceStatus | None, Query()] = None,
) -> Page[ResultToVerify]:
    not_implemented()


@router.post(
    "/admin/projects/{id}/result/verify",
    status_code=status.HTTP_200_OK,
    summary="Verify or reject a claimed place or award",
)
async def verify_result(id: ProjectId, body: VerifyResultBody, actor: AdminAuth) -> ProjectResult:
    not_implemented()


@router.get(
    "/projects/{id}/result",
    status_code=status.HTTP_200_OK,
    summary="Get result with retrospective restricted to team and admins",
)
async def get_result(id: ProjectId, actor: MemberAuth) -> ProjectResult:
    not_implemented()


@router.get(
    "/me/reviews/pending",
    status_code=status.HTTP_200_OK,
    summary="List open review windows and outstanding reviews",
)
async def list_pending_reviews(
    actor: MemberAuth,
) -> list[PendingReviewWindow]:
    not_implemented()


@router.put(
    "/review-windows/{id}/reviews/{reviewee_id}",
    status_code=status.HTTP_200_OK,
    summary="Submit or edit a participant review",
)
async def update_review(
    id: ReviewWindowId,
    reviewee_id: UserId,
    body: UpdateReviewBody,
    actor: MemberAuth,
) -> Review:
    not_implemented()


@router.put(
    "/events/{id}/host-reviews/{reviewee_id}",
    status_code=status.HTTP_200_OK,
    summary="Rate a host or speaker as an attendee",
)
async def update_host_review(
    id: EventId,
    reviewee_id: UserId,
    body: UpdateHostReviewBody,
    actor: MemberAuth,
) -> Review:
    not_implemented()


@router.get(
    "/users/{id}/reviews",
    status_code=status.HTTP_200_OK,
    summary="List revealed non-removed reviews",
)
async def list_user_reviews(
    id: UserId,
    pagination: Annotated[CursorParams, Depends()],
    actor: MemberAuth,
    context: Annotated[ReviewContext | None, Query()] = None,
) -> UserReviews:
    not_implemented()


@router.post(
    "/reviews/{id}/report", status_code=status.HTTP_201_CREATED, summary="Report a revealed review"
)
async def report_review(id: ReviewId, body: ReportReviewBody, actor: MemberAuth) -> ReviewReport:
    not_implemented()


@router.get("/admin/reviews", status_code=status.HTTP_200_OK, summary="List reviews for moderation")
async def list_admin_reviews(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    reported: Annotated[bool | None, Query()] = None,
) -> Page[AdminReview]:
    not_implemented()


@router.post(
    "/admin/reviews/{id}/remove",
    status_code=status.HTTP_200_OK,
    summary="Soft-remove a review with a reason",
)
async def remove_review(id: ReviewId, body: RemoveReviewBody, actor: AdminAuth) -> AdminReview:
    not_implemented()
