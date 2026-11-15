from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from hmm.common import MemberReviewStatus, QuizId, UserId
from hmm.onboarding import member_review, submissions
from hmm.onboarding import quizzes as onboarding_quizzes
from hmm.onboarding.schemas import (
    CompleteMemberReviewBody,
    CreateQuizBody,
    MemberReview,
    MemberReviewDetail,
    MemberReviewPage,
    Onboarding,
    OnboardingSubmission,
    Quiz,
    QuizValidation,
    UpdateAnswersBody,
    UpdateMemberReviewBody,
    UpdateQuizBody,
)
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES
from hmm.platform.jobs import Jobs
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import ReasonBody
from hmm.platform.security import AdminAuth, UserAuth
from hmm.platform.storage import Storage

router = APIRouter(tags=["onboarding"], responses=PROBLEM_RESPONSES)


@router.get(
    "/onboarding", status_code=status.HTTP_200_OK, summary="Get onboarding definition and draft"
)
async def get_onboarding(actor: UserAuth, session: Session) -> Onboarding:
    return await submissions.get_onboarding(session, actor)


@router.put(
    "/onboarding/answers", status_code=status.HTTP_200_OK, summary="Autosave onboarding answers"
)
async def update_answers(body: UpdateAnswersBody, actor: UserAuth, session: Session) -> Onboarding:
    return await submissions.update_answers(session, actor, body)


@router.post(
    "/onboarding/submit",
    status_code=status.HTTP_200_OK,
    summary="Submit onboarding and unlock access",
)
async def submit_onboarding(
    actor: UserAuth, session: Session, clock: CurrentClock, jobs: Jobs
) -> Onboarding:
    return await submissions.submit_onboarding(session, jobs, actor, clock.now())


@router.get("/admin/quizzes", status_code=status.HTTP_200_OK, summary="List quiz versions")
async def list_quizzes(actor: AdminAuth, session: Session) -> list[Quiz]:
    return await onboarding_quizzes.list_quizzes(session)


@router.post(
    "/admin/quizzes", status_code=status.HTTP_201_CREATED, summary="Create empty quiz draft"
)
async def create_quiz(body: CreateQuizBody, actor: AdminAuth, session: Session) -> Quiz:
    return await onboarding_quizzes.create_quiz(session, actor, body.title, body.reason)


@router.post(
    "/admin/quizzes/{id}/clone",
    status_code=status.HTTP_201_CREATED,
    summary="Clone quiz into a draft",
)
async def clone_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> Quiz:
    return await onboarding_quizzes.clone_quiz(session, actor, id, body.reason if body else None)


@router.get("/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Get full quiz tree")
async def get_quiz(id: QuizId, actor: AdminAuth, session: Session) -> Quiz:
    return await onboarding_quizzes.get_quiz(session, id)


@router.put(
    "/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Replace draft quiz tree"
)
async def update_quiz(id: QuizId, body: UpdateQuizBody, actor: AdminAuth, session: Session) -> Quiz:
    return await onboarding_quizzes.replace_quiz(session, actor, id, body)


@router.post(
    "/admin/quizzes/{id}/validate", status_code=status.HTTP_200_OK, summary="Validate quiz tree"
)
async def validate_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> QuizValidation:
    return await onboarding_quizzes.validate_quiz(session, actor, id, body.reason if body else None)


@router.post(
    "/admin/quizzes/{id}/publish", status_code=status.HTTP_200_OK, summary="Publish and freeze quiz"
)
async def publish_quiz(
    id: QuizId,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> Quiz:
    return await onboarding_quizzes.publish_quiz(
        session, actor, id, body.reason if body else None, clock.now()
    )


@router.delete(
    "/admin/quizzes/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete draft quiz"
)
async def delete_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> None:
    await onboarding_quizzes.delete_quiz(session, actor, id, body.reason if body else None)


@router.get(
    "/admin/users/{id}/onboarding",
    status_code=status.HTTP_200_OK,
    summary="Get onboarding submission history",
)
async def get_user_onboarding(
    id: UserId, actor: AdminAuth, session: Session
) -> list[OnboardingSubmission]:
    return await submissions.user_onboarding_history(session, id)


@router.post(
    "/admin/users/{id}/onboarding/reopen",
    status_code=status.HTTP_200_OK,
    summary="Reopen onboarding without locking access",
)
async def reopen_onboarding(
    id: UserId,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> Onboarding:
    return await submissions.reopen_onboarding(
        session, id, actor, body.reason if body is not None else None, clock.now()
    )


@router.get("/admin/member-reviews", status_code=status.HTTP_200_OK, summary="List member reviews")
async def list_member_reviews(
    actor: AdminAuth,
    session: Session,
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[MemberReviewStatus | None, Query()] = None,
) -> MemberReviewPage:
    return await member_review.member_reviews_page(session, pagination, status)


@router.get(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Get full member review bundle",
)
async def get_member_review(
    user_id: UserId, actor: AdminAuth, session: Session, storage: Storage
) -> MemberReviewDetail:
    return await member_review.member_review_detail(session, storage, user_id, actor)


@router.post(
    "/admin/member-reviews/{user_id}/complete",
    status_code=status.HTTP_200_OK,
    summary="Mark member reviewed",
)
async def complete_member_review(
    user_id: UserId,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: CompleteMemberReviewBody | None = None,
) -> MemberReview:
    return await member_review.complete_member_review(
        session,
        actor,
        user_id,
        body.note if body is not None else None,
        body.reason if body is not None else None,
        clock.now(),
    )


@router.patch(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Update private admin note",
)
async def update_member_review(
    user_id: UserId, body: UpdateMemberReviewBody, actor: AdminAuth, session: Session
) -> MemberReview:
    return await member_review.update_member_review(session, actor, user_id, body.note, body.reason)
