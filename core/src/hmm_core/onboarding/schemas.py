from enum import StrEnum

from hmm_contracts.algo import GithubSummary, OnboardingAnswer, QuizQuestion, QuizSection
from hmm_contracts.common import (
    CvTextStatus,
    MemberReviewStatus,
    OnboardingStatus,
    OnboardingSubmissionId,
    Page,
    QuizId,
    QuizStatus,
    SubmissionStatus,
    UserId,
    ValidationIssue,
)
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.platform.schemas import (
    ApiModel,
    Note,
    Title,
    UserRef,
    WithAllowedActions,
)
from hmm_core.scores.schemas import MemberAreaScore


class QuizAction(StrEnum):
    EDIT = "edit"
    VALIDATE = "validate"
    PUBLISH = "publish"
    CLONE = "clone"
    DELETE = "delete"


class OnboardingAction(StrEnum):
    ANSWER = "answer"
    SUBMIT = "submit"
    REOPEN = "reopen"


class MemberReviewAction(StrEnum):
    EDIT = "edit"
    COMPLETE = "complete"


class Quiz(WithAllowedActions[QuizAction]):
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


class CreateQuizBody(ApiModel):
    title: Title
    reason: Note | None = None


class UpdateQuizBody(ApiModel):
    title: Title
    sections: list[QuizSection]
    questions: list[QuizQuestion]
    reason: Note | None = None


class UpdateAnswersBody(ApiModel):
    answers: dict[str, OnboardingAnswer]


class OnboardingCv(ApiModel):
    present: bool
    text_status: CvTextStatus | None
    text_error: str | None


class Onboarding(WithAllowedActions[OnboardingAction]):
    status: OnboardingStatus
    quiz: Quiz | None
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


class MemberReview(WithAllowedActions[MemberReviewAction]):
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


class CompleteMemberReviewBody(ApiModel):
    note: Note | None = None
    reason: Note | None = None


class UpdateMemberReviewBody(ApiModel):
    note: Note
    reason: Note | None = None


class MemberReviewPage(Page[MemberReview]):
    pass
