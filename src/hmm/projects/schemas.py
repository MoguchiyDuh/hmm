from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from hmm.common import (
    AreaKey,
    EventId,
    InviteId,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestId,
    JoinRequestStatus,
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
from hmm.platform.schemas import (
    ApiModel,
    Links,
    Markdown,
    Name,
    Note,
    ProjectRef,
    Title,
    UserRef,
    WithAllowedActions,
)


class ProjectMemberAction(StrEnum):
    EDIT = "edit"
    REMOVE = "remove"
    MOVE = "move"
    CONFIRM_PLACEMENT = "confirm_placement"
    DECLINE_PLACEMENT = "decline_placement"
    TRANSFER_LEAD = "transfer_lead"
    CONFIRM = "confirm"


class InviteAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    WITHDRAW = "withdraw"


class JoinRequestAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    WITHDRAW = "withdraw"


class ProjectAction(StrEnum):
    EDIT = "edit"
    INVITE = "invite"
    REQUEST_JOIN = "request_join"
    LEAVE = "leave"
    ADD_MEMBER = "add_member"
    CLOSE_SEATS = "close_seats"
    REOPEN_SEATS = "reopen_seats"
    START = "start"
    PAUSE = "pause"
    ABANDON = "abandon"
    CONTINUE = "continue"
    TRANSFER_LEAD = "transfer_lead"
    ADD_MILESTONE = "add_milestone"
    POST_UPDATE = "post_update"
    RELEASE = "release"
    SUBMIT_RESULT = "submit_result"
    DISBAND = "disband"
    SET_STATUS = "set_status"
    SET_LEAD = "set_lead"


class MilestoneAction(StrEnum):
    EDIT = "edit"
    DELETE = "delete"


class Recruiting(ApiModel):
    areas: list[AreaKey]
    note: str


class RecruitingInput(ApiModel):
    areas: list[AreaKey]
    note: Note


class CreateProjectBody(ApiModel):
    name: Name
    description_md: Markdown
    goal: Note
    area_keys: list[AreaKey]
    links: Links
    event_id: EventId | None = None
    visibility: ProjectVisibility = ProjectVisibility.PUBLIC


class UpdateProjectBody(ApiModel):
    name: Name | None = None
    description_md: Markdown | None = None
    goal: Note | None = None
    area_keys: list[AreaKey] | None = None
    links: Links | None = None
    visibility: ProjectVisibility | None = None
    recruiting: RecruitingInput | None = None


class ProjectMember(WithAllowedActions[ProjectMemberAction]):
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
    accept_by: AwareDatetime | None
    is_team_row: bool
    joined_at: AwareDatetime
    confirmed_at: AwareDatetime | None
    left_at: AwareDatetime | None
    left_reason: LeftReason | None


class CreateProjectInviteBody(ApiModel):
    user_id: UserId
    message: Note | None = None
    area_key: AreaKey | None = None


class ProjectInvite(WithAllowedActions[InviteAction]):
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


class CreateProjectJoinRequestBody(ApiModel):
    note: Note


class JoinRequest(WithAllowedActions[JoinRequestAction]):
    id: JoinRequestId
    project_id: ProjectId
    project: ProjectRef
    user_id: UserId
    user: UserRef
    note: str
    status: JoinRequestStatus
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ProjectListing(WithAllowedActions[ProjectAction]):
    view: Literal["limited"]
    id: ProjectId
    name: str
    area_keys: list[AreaKey]


class ProjectSummary(ProjectRef, WithAllowedActions[ProjectAction]):
    view: Literal["full"]
    area_keys: list[AreaKey]
    status: ProjectStatus
    visibility: ProjectVisibility
    lead_user_id: UserId | None
    recruiting: Recruiting | None
    origin: ProjectOrigin
    created_at: AwareDatetime
    updated_at: AwareDatetime


type ProjectListView = Annotated[ProjectSummary | ProjectListing, Field(discriminator="view")]


class Project(ProjectSummary):
    description_md: str
    goal: str
    links: list[HttpUrl]
    seats_closed_at: AwareDatetime | None
    open_seats: int | None = Field(ge=0)
    last_progress_at: AwareDatetime | None
    members: list[ProjectMember]
    pending_invites: list[ProjectInvite] | None
    pending_join_requests: list[JoinRequest] | None


type ProjectView = Annotated[Project | ProjectListing, Field(discriminator="view")]


class CreateMilestoneBody(ApiModel):
    title: Title
    description: Note | None = None
    due_on: date | None = None
    position: int


class UpdateMilestoneBody(ApiModel):
    title: Title | None = None
    description: Note | None = None
    due_on: date | None = None
    position: int | None = None
    done: bool | None = None


class Milestone(WithAllowedActions[MilestoneAction]):
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


class CreateProjectUpdateBody(ApiModel):
    body_md: Markdown
    links: Links = Field(default_factory=list)


class ProjectUpdate(ApiModel):
    id: ProjectUpdateId
    project_id: ProjectId
    author_id: UserId
    body_md: str = Field(max_length=5000)
    links: list[HttpUrl] = Field(max_length=10)
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None


class ReleaseContribution(ApiModel):
    user_id: UserId
    contributed: bool
    area_key: AreaKey | None = None


class CreateProjectReleaseBody(ApiModel):
    title: Title
    notes_md: Markdown
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


class ProjectListViewPage(Page[ProjectListView]):
    pass
