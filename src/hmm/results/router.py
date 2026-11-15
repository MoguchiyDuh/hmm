from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from hmm import models
from hmm.common import (
    EventId,
    PlaceStatus,
    ProjectId,
    ReviewContext,
    ReviewId,
    ReviewWindowId,
    UserId,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.jobs import Jobs
from hmm.platform.pagination import CursorParams
from hmm.platform.security import AdminAuth, MemberAuth
from hmm.results import reviews, submissions, verification
from hmm.results.schemas import (
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
    UpdateHostReviewBody,
    UpdateResultBody,
    UpdateReviewBody,
    UserReviews,
    VerifyResultBody,
)

router = APIRouter(tags=["results"], responses=PROBLEM_RESPONSES)


@router.post(
    "/projects/{id}/result", status_code=status.HTTP_201_CREATED, summary="Submit hackathon result"
)
async def create_result(
    id: ProjectId,
    body: CreateResultBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    jobs: Jobs,
) -> ProjectResult:
    return await submissions.submit_result(
        session, jobs, id, body, actor, clock.now(), admin=actor.is_admin
    )


@router.patch(
    "/projects/{id}/result",
    status_code=status.HTTP_200_OK,
    summary="Edit result within the review window",
)
async def update_result(
    id: ProjectId,
    body: UpdateResultBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    jobs: Jobs,
) -> ProjectResult:
    return await submissions.update_result(
        session, jobs, id, body, actor, clock.now(), admin=actor.is_admin
    )


@router.get(
    "/admin/results",
    status_code=status.HTTP_200_OK,
    summary="List results to verify with parser winner candidates",
)
async def list_results_to_verify(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    place_status: Annotated[PlaceStatus | None, Query()] = None,
) -> ResultToVerifyPage:
    return await verification.result_verify_page(
        session, pagination, actor, clock.now(), place_status=place_status
    )


@router.post(
    "/admin/projects/{id}/result/verify",
    status_code=status.HTTP_200_OK,
    summary="Verify or reject a claimed place or award",
)
async def verify_result(
    id: ProjectId,
    body: VerifyResultBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    jobs: Jobs,
) -> ProjectResult:
    return await verification.verify_result(session, jobs, id, body, actor, clock.now())


@router.get(
    "/projects/{id}/result",
    status_code=status.HTTP_200_OK,
    summary="Get result with retrospective restricted to team and admins",
)
async def get_result(
    id: ProjectId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> ProjectResult:
    return await submissions.fetch_result(session, id, actor, clock.now(), admin=actor.is_admin)


@router.get(
    "/me/reviews/pending",
    status_code=status.HTTP_200_OK,
    summary="List open review windows and outstanding reviews",
)
async def list_pending_reviews(
    actor: MemberAuth, session: Session, clock: CurrentClock
) -> list[PendingReviewWindow]:
    return await reviews.pending_windows(session, actor=actor, now=clock.now())


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
    context: Annotated[ReviewContext | None, Query()] = None,
) -> UserReviews:
    return await reviews.user_reviews(
        session, target_id=id, actor_id=actor.id, context=context, params=pagination
    )


@router.post(
    "/reviews/{id}/report", status_code=status.HTTP_201_CREATED, summary="Report a revealed review"
)
async def report_review(
    id: ReviewId, body: ReportReviewBody, actor: MemberAuth, session: Session, clock: CurrentClock
) -> ReviewReport:
    report = await reviews.report_review(
        session, actor_id=actor.id, review_id=id, reason=body.reason, now=clock.now()
    )
    return reviews.build_report(report)


@router.get("/admin/reviews", status_code=status.HTTP_200_OK, summary="List reviews for moderation")
async def list_admin_reviews(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    session: Session,
    reported: Annotated[bool | None, Query()] = None,
) -> AdminReviewPage:
    return await reviews.admin_review_page(session, reported=reported, params=pagination)


@router.post(
    "/admin/reviews/{id}/remove",
    status_code=status.HTTP_200_OK,
    summary="Soft-remove a review with a reason",
)
async def remove_review(
    id: ReviewId, body: RemoveReviewBody, actor: AdminAuth, session: Session, clock: CurrentClock
) -> AdminReview:
    review = await reviews.remove_review(
        session, actor_id=actor.id, review_id=id, reason=body.reason, now=clock.now()
    )
    reviewer = await session.get(models.User, review.reviewer_id)
    reports = await session.scalars(
        select(models.ReviewReport)
        .where(models.ReviewReport.review_id == id)
        .order_by(models.ReviewReport.created_at, models.ReviewReport.id)
    )
    return AdminReview(
        **reviews.build_review(review, reviewer, []).model_dump(),
        reports=[reviews.build_report(report) for report in reports],
    )
