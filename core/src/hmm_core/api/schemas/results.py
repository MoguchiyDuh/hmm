from typing import Annotated, Literal

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

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    ProjectRef,
    Reason,
    UserRef,
    WithAllowedActions,
)


class CreateResultBody(ApiInput):
    outcome: ResultOutcome
    place: int | None = Field(default=None, ge=1)
    award: str | None = None
    repo_url: HttpUrl | None = None
    demo_url: HttpUrl | None = None
    presentation_url: HttpUrl | None = None
    member_areas: dict[UserId, AreaKey]
    participants: dict[UserId, bool]
    retrospective_md: str


class UpdateResultBody(ApiInput):
    outcome: ResultOutcome | None = None
    place: int | None = Field(default=None, ge=1)
    award: str | None = None
    repo_url: HttpUrl | None = None
    demo_url: HttpUrl | None = None
    presentation_url: HttpUrl | None = None
    member_areas: dict[UserId, AreaKey] | None = None
    participants: dict[UserId, bool] | None = None
    retrospective_md: str | None = None


class ProjectResult(WithAllowedActions):
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


class VerifyResultBody(Reason):
    decision: Literal["verify", "reject"]
    note: str | None = None


class UpdateReviewBody(ApiInput):
    rating: int | None = Field(default=None, ge=1, le=10)
    comment: str = Field(max_length=2000)


class UpdateHostReviewBody(ApiInput):
    rating: int = Field(ge=1, le=10)
    comment: str = Field(max_length=2000)


class Review(WithAllowedActions):
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


class PendingReviewWindow(WithAllowedActions):
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


class ReportReviewBody(ApiInput):
    reason: str = Field(min_length=1)


class ReviewReport(WithAllowedActions):
    id: ReviewReportId
    review_id: ReviewId
    reporter_id: UserId
    reason: str
    status: ReviewReportStatus
    created_at: AwareDatetime


class AdminReview(Review):
    reports: list[ReviewReport]


class RemoveReviewBody(ApiInput):
    reason: Annotated[str, Field(min_length=1)]
