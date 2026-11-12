from hmm_contracts.algo import FormationPlan, FormationRequest
from hmm_contracts.common import (
    AreaKey,
    ConfirmKind,
    EnrollmentId,
    EnrollmentSource,
    EnrollmentStatus,
    EventId,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunId,
    FormationRunStatus,
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
    MergeProposalId,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectId,
    ProjectMemberId,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    SwapRequestId,
    SwapRequestStatus,
    TeamStatus,
    Trust,
    UserId,
)
from pydantic import AwareDatetime, Field, JsonValue

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    EventRef,
    ProjectRef,
    Reason,
    UserRef,
    WithAllowedActions,
)


class AreaScoreSummary(ApiModel):
    area_key: AreaKey
    effective: float = Field(ge=0, le=10)


class Enrollment(WithAllowedActions):
    id: EnrollmentId
    event_id: EventId
    event: EventRef
    user: UserRef
    preferred_area_key: AreaKey | None
    note: str = Field(max_length=1000)
    willing_to_lead: bool
    status: EnrollmentStatus
    source: EnrollmentSource
    reserve_rank: int | None = Field(ge=1)
    status_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    top_scores: list[AreaScoreSummary]
    team: ProjectRef | None


class CreateEnrollmentBody(ApiInput):
    preferred_area_key: AreaKey | None = None
    note: str = Field(default="", max_length=1000)
    willing_to_lead: bool = False


class UpdateEnrollmentBody(ApiInput):
    preferred_area_key: AreaKey | None = None
    note: str | None = Field(default=None, max_length=1000)
    willing_to_lead: bool | None = None


class LeadFit(ApiModel):
    fit: float = Field(ge=0, le=1)
    eligible: bool
    reason: str
    computed_at: AwareDatetime


class LeadCandidate(LeadFit):
    user: UserRef
    willing_to_lead: bool
    tier: int = Field(ge=1, le=4)
    enrolled_at: AwareDatetime | None


class ProjectMember(WithAllowedActions):
    id: ProjectMemberId
    project_id: ProjectId
    event_id: EventId | None
    user: UserRef
    role: MemberRole
    area_key: AreaKey | None
    state: MemberState
    trust: Trust
    joined_via: JoinedVia
    proposal_reason: str | None
    confirm_by: AwareDatetime | None
    confirm_kind: ConfirmKind | None
    joined_at: AwareDatetime
    confirmed_at: AwareDatetime | None
    left_at: AwareDatetime | None
    left_reason: LeftReason | None


class TeamRecruiting(ApiModel):
    areas: list[AreaKey]
    note: str


class Team(ProjectRef, WithAllowedActions):
    description_md: str
    goal: str
    links: list[str]
    visibility: ProjectVisibility
    area_keys: list[AreaKey]
    recruiting: TeamRecruiting | None
    lead_user_id: UserId | None
    status: ProjectStatus
    origin: ProjectOrigin
    members: list[ProjectMember]
    ready_at: AwareDatetime | None
    review_ends_at: AwareDatetime | None
    formed_in_run_id: FormationRunId | None
    locked_at: AwareDatetime | None
    seat_open_since: AwareDatetime | None
    seats_closed_at: AwareDatetime | None
    open_seats: int = Field(ge=0)
    last_lead_action_at: AwareDatetime | None
    last_progress_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class FormationCounts(ApiModel):
    enrollments: int = Field(ge=0)
    active: int = Field(ge=0)
    placed: int = Field(ge=0)
    reserve: int = Field(ge=0)
    cores: int = Field(ge=0)
    core_members: int = Field(ge=0)
    teams: int = Field(ge=0)
    volunteers: int = Field(ge=0)


class FormationProjection(ApiModel):
    teams: int = Field(ge=0)
    new_teams: int = Field(ge=0)
    placed: int = Field(ge=0)
    reserve: int = Field(ge=0)
    undersized_cores: int = Field(ge=0)
    team_sizes: list[int]
    volunteers: int = Field(ge=0)


class ParticipationState(ApiModel):
    enrollment: Enrollment | None
    team: ProjectRef | None
    membership: ProjectMember | None
    lead_fit: LeadFit | None


class Formation(WithAllowedActions):
    event_id: EventId
    phase: FormationPhase
    target: int = Field(ge=1, le=20)
    viable: int = Field(ge=1, le=20)
    min_size: int = Field(ge=1, le=20)
    max_size: int = Field(ge=1, le=20)
    core_max: int = Field(ge=2, le=20)
    target_override: int | None = Field(ge=1, le=20)
    formation_at_override: AwareDatetime | None
    formation_at: AwareDatetime
    review_ends_at: AwareDatetime | None
    team_deadline: AwareDatetime
    reg_deadline: AwareDatetime
    rounds: int = Field(ge=0)
    last_run_id: FormationRunId | None
    counts: FormationCounts
    projection: FormationProjection
    my_state: ParticipationState


class CreateTeamBody(ApiInput):
    name: str = Field(min_length=1)
    description: str | None = None


class AdminCreateTeamBody(Reason):
    lead_user_id: UserId
    name: str = Field(min_length=1)


class Invite(WithAllowedActions):
    id: InviteId
    project: ProjectRef
    event_id: EventId | None
    user: UserRef
    invited_by: UserRef | None
    kind: InviteKind
    message: str | None
    area_key: AreaKey | None
    status: InviteStatus
    closed_reason: str | None
    expires_at: AwareDatetime
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class JoinRequest(WithAllowedActions):
    id: JoinRequestId
    project: ProjectRef
    event_id: EventId | None
    user: UserRef
    note: str
    status: JoinRequestStatus
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class LeadOffer(WithAllowedActions):
    id: LeadOfferId
    project: ProjectRef
    event_id: EventId | None
    user: UserRef
    source: LeadOfferSource
    offered_by: UserRef | None
    tier: int = Field(ge=1, le=4)
    reason: str
    status: LeadOfferStatus
    expires_at: AwareDatetime
    responded_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class DeclinePlacementBody(ApiInput):
    withdraw: bool


class TransferLeadBody(ApiInput):
    user_id: UserId


class AddMemberBody(ApiInput):
    user_id: UserId
    area_key: AreaKey | None = None


class AdminAddMemberBody(AddMemberBody):
    reason: str | None = None


class UpdateMemberBody(ApiInput):
    area_key: AreaKey | None


class CreateSwapBody(ApiInput):
    from_user_id: UserId
    to_user_id: UserId


class Swap(WithAllowedActions):
    id: SwapRequestId
    event_id: EventId
    from_team_id: ProjectId
    from_user_id: UserId
    to_team_id: ProjectId
    to_user_id: UserId
    requested_by: UserId
    to_lead_accepted_at: AwareDatetime | None
    from_user_accepted_at: AwareDatetime | None
    to_user_accepted_at: AwareDatetime | None
    status: SwapRequestStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateMergeBody(ApiInput):
    target_team_id: ProjectId


class Merge(WithAllowedActions):
    id: MergeProposalId
    event_id: EventId
    run_id: FormationRunId | None
    absorbed_team_id: ProjectId
    target_team_id: ProjectId
    proposed_by: MergeProposedBy
    absorbed_accepted_at: AwareDatetime | None
    target_accepted_at: AwareDatetime | None
    status: MergeProposalStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UpdateFormationBody(Reason):
    target_override: int | None = Field(default=None, ge=1, le=20)
    formation_at_override: AwareDatetime | None = None


class FormationRun(WithAllowedActions):
    id: FormationRunId
    event_id: EventId
    round: int = Field(ge=1)
    kind: FormationKind
    status: FormationRunStatus
    planner: FormationPlanner | None
    input: FormationRequest | None
    output: FormationPlan | None
    validation_errors: list[JsonValue]
    algo_version: str | None
    created_at: AwareDatetime
    applied_at: AwareDatetime | None


class UpdateReserveOrderBody(Reason):
    user_ids: list[UserId] = Field(min_length=1)


class MoveMemberBody(Reason):
    user_id: UserId
    to_team_id: ProjectId


class AssignLeadBody(Reason):
    user_id: UserId


class UpdateTeamStatusBody(Reason):
    status: TeamStatus
