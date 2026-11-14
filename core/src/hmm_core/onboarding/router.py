from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import MemberReviewStatus, QuizId, UserId

from hmm_core.onboarding import service
from hmm_core.onboarding.schemas import (
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
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.security import AdminAuth, UserAuth

router = APIRouter(tags=["onboarding"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["onboarding"], responses=PROBLEM_RESPONSES)


@router.get(
    "/onboarding", status_code=status.HTTP_200_OK, summary="Get onboarding definition and draft"
)
async def get_onboarding(actor: UserAuth) -> Onboarding:
    not_implemented()


@router.put(
    "/onboarding/answers", status_code=status.HTTP_200_OK, summary="Autosave onboarding answers"
)
async def update_answers(body: UpdateAnswersBody, actor: UserAuth) -> Onboarding:
    not_implemented()


@router.post(
    "/onboarding/submit",
    status_code=status.HTTP_200_OK,
    summary="Submit onboarding and unlock access",
)
async def submit_onboarding(actor: UserAuth) -> Onboarding:
    not_implemented()


@router.get("/admin/quizzes", status_code=status.HTTP_200_OK, summary="List quiz versions")
async def list_quizzes(actor: AdminAuth, session: Session) -> list[Quiz]:
    return await service.list_quizzes(session)


@router.post(
    "/admin/quizzes", status_code=status.HTTP_201_CREATED, summary="Create empty quiz draft"
)
async def create_quiz(body: CreateQuizBody, actor: AdminAuth, session: Session) -> Quiz:
    return await service.create_quiz(session, actor, body.title, body.reason)


@router.post(
    "/admin/quizzes/{id}/clone",
    status_code=status.HTTP_201_CREATED,
    summary="Clone quiz into a draft",
)
async def clone_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> Quiz:
    return await service.clone_quiz(session, actor, id, body.reason if body else None)


@router.get("/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Get full quiz tree")
async def get_quiz(id: QuizId, actor: AdminAuth, session: Session) -> Quiz:
    return await service.get_quiz(session, id)


@router.put(
    "/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Replace draft quiz tree"
)
async def update_quiz(id: QuizId, body: UpdateQuizBody, actor: AdminAuth, session: Session) -> Quiz:
    return await service.replace_quiz(session, actor, id, body)


@router.post(
    "/admin/quizzes/{id}/validate", status_code=status.HTTP_200_OK, summary="Validate quiz tree"
)
async def validate_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> QuizValidation:
    return await service.validate_quiz(session, actor, id, body.reason if body else None)


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
    return await service.publish_quiz(
        session, actor, id, body.reason if body else None, clock.now()
    )


@router.delete(
    "/admin/quizzes/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete draft quiz"
)
async def delete_quiz(
    id: QuizId, actor: AdminAuth, session: Session, body: ReasonBody | None = None
) -> None:
    await service.delete_quiz(session, actor, id, body.reason if body else None)


@router.get(
    "/admin/users/{id}/onboarding",
    status_code=status.HTTP_200_OK,
    summary="Get onboarding submission history",
)
async def get_user_onboarding(id: UserId, actor: AdminAuth) -> list[OnboardingSubmission]:
    not_implemented()


@router.post(
    "/admin/users/{id}/onboarding/reopen",
    status_code=status.HTTP_200_OK,
    summary="Reopen onboarding without locking access",
)
async def reopen_onboarding(
    id: UserId, actor: AdminAuth, body: ReasonBody | None = None
) -> Onboarding:
    not_implemented()


@router.get("/admin/member-reviews", status_code=status.HTTP_200_OK, summary="List member reviews")
async def list_member_reviews(
    actor: AdminAuth,
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[MemberReviewStatus | None, Query()] = None,
) -> MemberReviewPage:
    not_implemented()


@router.get(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Get full member review bundle",
)
async def get_member_review(user_id: UserId, actor: AdminAuth) -> MemberReviewDetail:
    not_implemented()


@router.post(
    "/admin/member-reviews/{user_id}/complete",
    status_code=status.HTTP_200_OK,
    summary="Mark member reviewed",
)
async def complete_member_review(
    user_id: UserId,
    actor: AdminAuth,
    body: CompleteMemberReviewBody | None = None,
) -> MemberReview:
    not_implemented()


@router.patch(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Update private admin note",
)
async def update_member_review(
    user_id: UserId, body: UpdateMemberReviewBody, actor: AdminAuth
) -> MemberReview:
    not_implemented()
