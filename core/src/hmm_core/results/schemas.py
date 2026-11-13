from enum import StrEnum
from typing import Literal

from hmm_contracts.common import (
    AreaKey,
    EventSourcePersonId,
    Page,
    PlaceStatus,
    ProjectId,
    ResultOutcome,
    ReviewContext,
    ReviewId,
    ReviewReportId,
    ReviewReportStatus,
    ReviewSubjectType,
    ReviewWindowId,
    ReviewWindowSubjectType,
    SubjectId,
    UserId,
)
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.platform.schemas import (
    ApiModel,
    Markdown,
    Note,
    ProjectRef,
    ReasonBody,
    Title,
    UserRef,
    WithAllowedActions,
)


class ResultAction(StrEnum):
    EDIT = "edit"
    VERIFY = "verify"


class ReviewAction(StrEnum):
    EDIT = "edit"
    REPORT = "report"
    REMOVE = "remove"


class ReviewWindowAction(StrEnum):
    REVIEW = "review"


class CreateResultBody(ApiModel):
    outcome: ResultOutcome
    place: int | None = Field(default=None, ge=1)
    award: Title | None = None
    repo_url: HttpUrl | None = None
    demo_url: HttpUrl | None = None
    presentation_url: HttpUrl | None = None
    member_areas: dict[UserId, AreaKey]
    participants: dict[UserId, bool]
    retrospective_md: Markdown


class UpdateResultBody(ApiModel):
    outcome: ResultOutcome | None = None
    place: int | None = Field(default=None, ge=1)
    award: Title | None = None
    repo_url: HttpUrl | None = None
    demo_url: HttpUrl | None = None
    presentation_url: HttpUrl | None = None
    member_areas: dict[UserId, AreaKey] | None = None
    participants: dict[UserId, bool] | None = None
    retrospective_md: Markdown | None = None


class ProjectResult(WithAllowedActions[ResultAction]):
    project_id: ProjectId
    outcome: ResultOutcome
    place: int | None = Field(ge=1)
    award: str | None
    place_status: PlaceStatus | None
    place_verified_by: UserId | None
    place_verified_at: AwareDatetime | None
    repo_url: HttpUrl | None
    demo_url: HttpUrl | None
    presentation_url: HttpUrl | None
    retrospective_md: str | None = None
    member_areas: dict[UserId, AreaKey]
    participants: dict[UserId, bool]
    auto_closed: bool
    submitted_by: UserId | None
    submitted_at: AwareDatetime | None
    edited_at: AwareDatetime | None


class WinnerCandidate(ApiModel):
    id: EventSourcePersonId
    source_id: str
    github_login: str | None
    telegram_username: str | None
    display_name: str
    place: int | None = Field(ge=1)
    matched_user_id: UserId | None


class ResultToVerify(ProjectResult):
    project: ProjectRef
    winner_candidates: list[WinnerCandidate]


class VerifyResultBody(ReasonBody):
    decision: Literal["verify", "reject"]
    note: Note | None = None


class UpdateReviewBody(ApiModel):
    rating: int | None = Field(default=None, ge=1, le=10)
    comment: str = Field(max_length=2000)


class UpdateHostReviewBody(ApiModel):
    rating: int = Field(ge=1, le=10)
    comment: str = Field(max_length=2000)


class Review(WithAllowedActions[ReviewAction]):
    id: ReviewId
    window_id: ReviewWindowId | None
    subject_type: ReviewSubjectType
    subject_id: SubjectId
    reviewer: UserRef | None
    reviewee_id: UserId
    context: ReviewContext
    rating: int | None = Field(ge=1, le=10)
    comment: str = Field(max_length=2000)
    revealed_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    removed_at: AwareDatetime | None
    removed_by: UserId | None
    removed_reason: str | None


class PendingReviewWindow(WithAllowedActions[ReviewWindowAction]):
    id: ReviewWindowId
    subject_type: ReviewWindowSubjectType
    subject_id: SubjectId
    subject_title: str
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    to_review: list[UserRef]
    submitted: list[Review]


class ReviewRatingSummary(ApiModel):
    context: ReviewContext
    average: float | None = Field(ge=1, le=10)
    count: int = Field(ge=0)


class UserReviews(Page[Review], ApiModel):
    reputation: list[ReviewRatingSummary]


class ReportReviewBody(ApiModel):
    reason: Note


class ReviewReport(ApiModel):
    id: ReviewReportId
    review_id: ReviewId
    reporter_id: UserId
    reason: str
    status: ReviewReportStatus
    created_at: AwareDatetime


class AdminReview(Review):
    reports: list[ReviewReport]


class RemoveReviewBody(ApiModel):
    reason: Note


class AdminReviewPage(Page[AdminReview]):
    pass


class ResultToVerifyPage(Page[ResultToVerify]):
    pass
