from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    EventId,
    PlaceStatus,
    ProjectId,
    ReviewContext,
    ReviewId,
    ReviewWindowId,
    ReviewWindowSubjectType,
    UserId,
)

from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import AdminAuth, MemberAuth
from hmm_core.results import reviews
from hmm_core.results.schemas import (
    AdminReview,
    AdminReviewPage,
    CreateResultBody,
    PendingReviewWindow,
    ProjectResult,
    RemoveReviewBody,
    ReportReviewBody,
    ResultToVerifyPage,
    Review,
    ReviewAction,
    ReviewReport,
    ReviewWindowAction,
    UpdateHostReviewBody,
    UpdateResultBody,
    UpdateReviewBody,
    UserReviews,
    VerifyResultBody,
)

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
) -> ResultToVerifyPage:
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
    actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[PendingReviewWindow]:
    rows = await reviews.pending_windows(session, actor_id=actor.id, now=clock.now())
    return [
        PendingReviewWindow(
            id=row.window.id,
            subject_type=ReviewWindowSubjectType(row.window.subject_type),
            subject_id=row.window.subject_id,
            subject_title=row.subject_title,
            opens_at=row.window.opens_at,
            closes_at=row.window.closes_at,
            to_review=[reviews.user_ref(user) for user in row.to_review],
            submitted=[
                reviews.build_review(review, actor, [ReviewAction.EDIT]) for review in row.submitted
            ],
            allowed_actions=[ReviewWindowAction.REVIEW],
        )
        for row in rows
    ]


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
    session: Session,
    clock: CurrentClock,
) -> Review:
    review = await reviews.submit_review(
        session,
        actor_id=actor.id,
        window_id=id,
        reviewee_id=reviewee_id,
        rating=body.rating,
        comment=body.comment,
        now=clock.now(),
    )
    return reviews.build_review(review, actor, [ReviewAction.EDIT])


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
    session: Session,
    clock: CurrentClock,
) -> Review:
    review = await reviews.submit_host_review(
        session,
        actor_id=actor.id,
        event_id=id,
        reviewee_id=reviewee_id,
        rating=body.rating,
        comment=body.comment,
        now=clock.now(),
    )
    return reviews.build_review(review, actor, [])


@router.get(
    "/users/{id}/reviews",
    status_code=status.HTTP_200_OK,
    summary="List revealed non-removed reviews",
)
async def list_user_reviews(
    id: UserId,
    pagination: Annotated[CursorParams, Depends()],
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    context: Annotated[ReviewContext | None, Query()] = None,
) -> UserReviews:
    page = await reviews.user_reviews(session, target_id=id, context=context, params=pagination)
    return UserReviews(
        items=[
            reviews.build_review(
                review,
                None
                if review.context in {ReviewContext.HOST.value, ReviewContext.SPEAKER.value}
                else page.users.get(review.reviewer_id),
                [] if review.reviewer_id == actor.id else [ReviewAction.REPORT],
            )
            for review in page.items
        ],
        next_cursor=page.next_cursor,
        reputation=page.reputation,
    )


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
) -> AdminReviewPage:
    not_implemented()


@router.post(
    "/admin/reviews/{id}/remove",
    status_code=status.HTTP_200_OK,
    summary="Soft-remove a review with a reason",
)
async def remove_review(id: ReviewId, body: RemoveReviewBody, actor: AdminAuth) -> AdminReview:
    not_implemented()
