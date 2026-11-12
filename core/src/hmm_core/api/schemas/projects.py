from datetime import date
from typing import Self

from hmm_contracts.common import (
    AreaKey,
    ConfirmKind,
    EventId,
    FormationRunId,
    InviteId,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestId,
    JoinRequestStatus,
    LeadOfferId,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    MilestoneId,
    Page,
    ProjectId,
    ProjectMemberId,
    ProjectOrigin,
    ProjectStatus,
    ProjectUpdateId,
    ProjectVisibility,
    ReleaseId,
    Trust,
    UserId,
)
from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from hmm_core.api.schemas.common import ApiInput, ApiModel, ProjectRef, UserRef, WithAllowedActions


class Recruiting(ApiModel):
    areas: list[AreaKey]
    note: str


class RecruitingInput(ApiInput):
    areas: list[AreaKey]
    note: str


class CreateProjectBody(ApiInput):
    name: str
    description_md: str
    goal: str
    area_keys: list[AreaKey]
    links: list[HttpUrl]
    event_id: EventId | None = None
    visibility: ProjectVisibility = ProjectVisibility.PUBLIC


class UpdateProjectBody(ApiInput):
    name: str | None = None
    description: str | None = None
    description_md: str | None = None
    goal: str | None = None
    area_keys: list[AreaKey] | None = None
    links: list[HttpUrl] | None = None
    visibility: ProjectVisibility | None = None
    recruiting: RecruitingInput | None = None


class ProjectMember(WithAllowedActions):
    id: ProjectMemberId
    project_id: ProjectId
    event_id: EventId | None
    user_id: UserId
    user: UserRef
    role: MemberRole
    area_key: AreaKey | None
    state: MemberState
    trust: Trust
    joined_via: JoinedVia
    proposal_reason: str | None
    confirm_by: AwareDatetime | None
    confirm_kind: ConfirmKind | None
    is_team_row: bool
    joined_at: AwareDatetime
    confirmed_at: AwareDatetime | None
    left_at: AwareDatetime | None
    left_reason: LeftReason | None


class CreateProjectInviteBody(ApiInput):
    user_id: UserId
    message: str | None = None
    area_key: AreaKey | None = None


class Invite(WithAllowedActions):
    id: InviteId
    project_id: ProjectId
    project: ProjectRef
    event_id: EventId | None
    user_id: UserId
    user: UserRef
    invited_by: UserId | None
    inviter: UserRef | None
    kind: InviteKind
    message: str | None
    area_key: AreaKey | None
    status: InviteStatus
    closed_reason: str | None
    expires_at: AwareDatetime
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateProjectJoinRequestBody(ApiInput):
    note: str


class JoinRequest(WithAllowedActions):
    id: JoinRequestId
    project_id: ProjectId
    project: ProjectRef
    event_id: EventId | None
    user_id: UserId
    user: UserRef
    note: str
    status: JoinRequestStatus
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class LeadOffer(WithAllowedActions):
    id: LeadOfferId
    project_id: ProjectId
    project: ProjectRef
    event_id: EventId | None
    user_id: UserId
    user: UserRef
    source: LeadOfferSource
    offered_by: UserId | None
    offerer: UserRef | None
    tier: int | None = Field(ge=1, le=4)
    reason: str
    status: LeadOfferStatus
    expires_at: AwareDatetime
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ProjectListing(WithAllowedActions):
    id: ProjectId
    name: str
    area_keys: list[AreaKey]


class ProjectSummary(ProjectRef, WithAllowedActions):
    area_keys: list[AreaKey]
    status: ProjectStatus
    visibility: ProjectVisibility
    lead_user_id: UserId | None
    recruiting: Recruiting | None
    origin: ProjectOrigin
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ProjectPage(Page[ProjectSummary | ProjectListing], ApiModel):
    pass


class Project(ProjectSummary):
    description_md: str
    goal: str
    links: list[HttpUrl]
    ready_at: AwareDatetime | None
    review_ends_at: AwareDatetime | None
    formed_in_run_id: FormationRunId | None
    locked_at: AwareDatetime | None
    seat_open_since: AwareDatetime | None
    seats_closed_at: AwareDatetime | None
    open_seats: int | None = Field(ge=0)
    last_lead_action_at: AwareDatetime | None
    last_progress_at: AwareDatetime | None
    members: list[ProjectMember]
    pending_invites: list[Invite] | None
    pending_join_requests: list[JoinRequest] | None
    pending_lead_offer: LeadOffer | None


class CreateMilestoneBody(ApiInput):
    title: str
    description: str | None = None
    due_on: date | None = None
    position: int


class UpdateMilestoneBody(ApiInput):
    title: str | None = None
    description: str | None = None
    due_on: date | None = None
    position: int | None = None
    done: bool | None = None


class Milestone(WithAllowedActions):
    id: MilestoneId
    project_id: ProjectId
    title: str
    description: str | None
    due_on: date | None
    position: int
    done_at: AwareDatetime | None
    done_by: UserId | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateProjectUpdateBody(ApiInput):
    body_md: str = Field(max_length=5000)
    links: list[HttpUrl] = Field(default_factory=list, max_length=10)


class ProjectUpdate(WithAllowedActions):
    id: ProjectUpdateId
    project_id: ProjectId
    author_id: UserId
    body_md: str = Field(max_length=5000)
    links: list[HttpUrl] = Field(max_length=10)
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None


class ReleaseContribution(ApiInput):
    user_id: UserId
    contributed: bool
    area_key: AreaKey | None = None


class CreateProjectReleaseBody(ApiInput):
    title: str
    notes_md: str
    repo_url: HttpUrl | None = None
    demo_url: HttpUrl | None = None
    writeup_url: HttpUrl | None = None
    contributions: list[ReleaseContribution]

    @model_validator(mode="after")
    def require_link(self) -> Self:
        if not any((self.repo_url, self.demo_url, self.writeup_url)):
            raise ValueError("At least one release link is required")
        return self


class ProjectRelease(ApiModel):
    id: ReleaseId
    project_id: ProjectId
    title: str
    notes_md: str
    repo_url: HttpUrl | None
    demo_url: HttpUrl | None
    writeup_url: HttpUrl | None
    contributions: list[ReleaseContribution]
    created_by: UserId
    created_at: AwareDatetime
