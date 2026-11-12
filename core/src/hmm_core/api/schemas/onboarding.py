from hmm_contracts.algo import GithubSummary, OnboardingAnswer, QuizQuestion, QuizSection
from hmm_contracts.common import (
    CvTextStatus,
    MemberReviewStatus,
    OnboardingStatus,
    OnboardingSubmissionId,
    QuizId,
    QuizStatus,
    SubmissionStatus,
    UserId,
    ValidationIssue,
)
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.api.schemas.common import ApiInput, ApiModel, UserRef, WithAllowedActions
from hmm_core.api.schemas.scores import MemberAreaScore


class Quiz(WithAllowedActions):
    id: QuizId
    version: int = Field(ge=1)
    title: str
    status: QuizStatus
    published_at: AwareDatetime | None
    created_by: UserId
    sections: list[QuizSection]
    questions: list[QuizQuestion]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateQuizBody(ApiInput):
    title: str
    reason: str | None = None


class UpdateQuizBody(ApiInput):
    title: str
    sections: list[QuizSection]
    questions: list[QuizQuestion]
    reason: str | None = None


class UpdateAnswersBody(ApiInput):
    answers: dict[str, OnboardingAnswer]


class OnboardingCv(ApiModel):
    present: bool
    text_status: CvTextStatus | None


class Onboarding(WithAllowedActions):
    status: OnboardingStatus
    quiz: Quiz
    draft_answers: dict[str, OnboardingAnswer]
    cv: OnboardingCv


class QuizValidation(ApiModel):
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


class OnboardingSubmission(ApiModel):
    id: OnboardingSubmissionId
    user_id: UserId
    quiz_id: QuizId
    quiz: Quiz
    answers: dict[str, OnboardingAnswer]
    status: SubmissionStatus
    submitted_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class MemberReview(WithAllowedActions):
    user: UserRef
    status: MemberReviewStatus
    opened_at: AwareDatetime
    reviewed_by: UserId | None
    reviewed_at: AwareDatetime | None
    admin_note: str | None
    discrepancies: list[str]


class ReviewCv(ApiModel):
    filename: str
    url: HttpUrl
    text: str | None = Field(max_length=100000)
    text_status: CvTextStatus
    text_error: str | None


class MemberReviewDetail(MemberReview):
    submissions: list[OnboardingSubmission]
    cv: ReviewCv | None
    github: GithubSummary | None
    area_scores: list[MemberAreaScore]


class CompleteMemberReviewBody(ApiInput):
    note: str | None = None
    reason: str | None = None


class UpdateMemberReviewBody(ApiInput):
    note: str
    reason: str | None = None
