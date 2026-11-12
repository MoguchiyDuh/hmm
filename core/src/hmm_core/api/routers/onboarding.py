from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import MemberReviewStatus, Page, QuizId, UserId

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.common import Reason
from hmm_core.api.schemas.onboarding import (
    CompleteMemberReviewBody,
    CreateQuizBody,
    MemberReview,
    MemberReviewDetail,
    Onboarding,
    OnboardingSubmission,
    Quiz,
    QuizValidation,
    UpdateAnswersBody,
    UpdateMemberReviewBody,
    UpdateQuizBody,
)
from hmm_core.api.security import admin, session_user

router = APIRouter(tags=["onboarding"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["onboarding"], responses=PROBLEM_RESPONSES)


@router.get(
    "/onboarding", status_code=status.HTTP_200_OK, summary="Get onboarding definition and draft"
)
async def get_onboarding(actor: Annotated[None, Depends(session_user)]) -> Onboarding:
    not_implemented()


@router.put(
    "/onboarding/answers", status_code=status.HTTP_200_OK, summary="Autosave onboarding answers"
)
async def update_answers(
    body: UpdateAnswersBody, actor: Annotated[None, Depends(session_user)]
) -> Onboarding:
    not_implemented()


@router.post(
    "/onboarding/submit",
    status_code=status.HTTP_200_OK,
    summary="Submit onboarding and unlock access",
)
async def submit_onboarding(actor: Annotated[None, Depends(session_user)]) -> Onboarding:
    not_implemented()


@router.get("/admin/quizzes", status_code=status.HTTP_200_OK, summary="List quiz versions")
async def list_quizzes(actor: Annotated[None, Depends(admin)]) -> list[Quiz]:
    not_implemented()


@router.post(
    "/admin/quizzes", status_code=status.HTTP_201_CREATED, summary="Create empty quiz draft"
)
async def create_quiz(body: CreateQuizBody, actor: Annotated[None, Depends(admin)]) -> Quiz:
    not_implemented()


@router.post(
    "/admin/quizzes/{id}/clone",
    status_code=status.HTTP_201_CREATED,
    summary="Clone quiz into a draft",
)
async def clone_quiz(
    id: QuizId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> Quiz:
    not_implemented()


@router.get("/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Get full quiz tree")
async def get_quiz(id: QuizId, actor: Annotated[None, Depends(admin)]) -> Quiz:
    not_implemented()


@router.put(
    "/admin/quizzes/{id}", status_code=status.HTTP_200_OK, summary="Replace draft quiz tree"
)
async def update_quiz(
    id: QuizId, body: UpdateQuizBody, actor: Annotated[None, Depends(admin)]
) -> Quiz:
    not_implemented()


@router.post(
    "/admin/quizzes/{id}/validate", status_code=status.HTTP_200_OK, summary="Validate quiz tree"
)
async def validate_quiz(
    id: QuizId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> QuizValidation:
    not_implemented()


@router.post(
    "/admin/quizzes/{id}/publish", status_code=status.HTTP_200_OK, summary="Publish and freeze quiz"
)
async def publish_quiz(
    id: QuizId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> Quiz:
    not_implemented()


@router.delete(
    "/admin/quizzes/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete draft quiz"
)
async def delete_quiz(
    id: QuizId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> None:
    not_implemented()


@router.get(
    "/admin/users/{id}/onboarding",
    status_code=status.HTTP_200_OK,
    summary="Get onboarding submission history",
)
async def get_user_onboarding(
    id: UserId, actor: Annotated[None, Depends(admin)]
) -> list[OnboardingSubmission]:
    not_implemented()


@router.post(
    "/admin/users/{id}/onboarding/reopen",
    status_code=status.HTTP_200_OK,
    summary="Reopen onboarding without locking access",
)
async def reopen_onboarding(
    id: UserId, actor: Annotated[None, Depends(admin)], body: Reason | None = None
) -> Onboarding:
    not_implemented()


@router.get("/admin/member-reviews", status_code=status.HTTP_200_OK, summary="List member reviews")
async def list_member_reviews(
    actor: Annotated[None, Depends(admin)],
    pagination: Annotated[CursorParams, Depends()],
    status: Annotated[MemberReviewStatus | None, Query()] = None,
) -> Page[MemberReview]:
    not_implemented()


@router.get(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Get full member review bundle",
)
async def get_member_review(
    user_id: UserId, actor: Annotated[None, Depends(admin)]
) -> MemberReviewDetail:
    not_implemented()


@router.post(
    "/admin/member-reviews/{user_id}/complete",
    status_code=status.HTTP_200_OK,
    summary="Mark member reviewed",
)
async def complete_member_review(
    user_id: UserId,
    actor: Annotated[None, Depends(admin)],
    body: CompleteMemberReviewBody | None = None,
) -> MemberReview:
    not_implemented()


@router.patch(
    "/admin/member-reviews/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Update private admin note",
)
async def update_member_review(
    user_id: UserId, body: UpdateMemberReviewBody, actor: Annotated[None, Depends(admin)]
) -> MemberReview:
    not_implemented()
