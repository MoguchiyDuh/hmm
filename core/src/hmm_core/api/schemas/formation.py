from enum import StrEnum

from hmm_contracts.algo import FormationPlan, FormationRequest
from hmm_contracts.common import (
    AreaKey,
    EnrollmentId,
    EnrollmentSource,
    EnrollmentStatus,
    EventId,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunId,
    FormationRunStatus,
    MergeProposalId,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectId,
    SwapRequestId,
    SwapRequestStatus,
    TeamStatus,
    UserId,
)
from pydantic import AwareDatetime, Field, JsonValue

from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    AreaScoreSummary,
    EventRef,
    Markdown,
    Name,
    Note,
    ProjectRef,
    ReasonBody,
    UserRef,
    WithAllowedActions,
)
from hmm_core.api.schemas.projects import ProjectMember


class EnrollmentAction(StrEnum):
    EDIT = "edit"
    WITHDRAW = "withdraw"
    INVITE = "invite"
    ADD = "add"


class FormationAction(StrEnum):
    ENROLL = "enroll"
    EDIT_ENROLLMENT = "edit_enrollment"
    WITHDRAW = "withdraw"
    CLAIM_LEAD = "claim_lead"
    EDIT = "edit"
    RUN = "run"
    CREATE_TEAM = "create_team"
    REORDER_RESERVE = "reorder_reserve"


class SwapAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"


class MergeAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"


class Enrollment(WithAllowedActions[EnrollmentAction]):
    id: EnrollmentId
    event_id: EventId
    event: EventRef
    user: UserRef
    preferred_area_key: AreaKey | None
    note: str | None
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
    note: Note | None = None
    willing_to_lead: bool = False


class UpdateEnrollmentBody(ApiInput):
    preferred_area_key: AreaKey | None = None
    note: Note | None = None
    willing_to_lead: bool | None = None


class LeadEligibility(ApiModel):
    fit: float = Field(ge=0, le=1)
    eligible: bool
    reason: str
    computed_at: AwareDatetime


class LeadCandidate(LeadEligibility):
    user: UserRef
    willing_to_lead: bool
    tier: int = Field(ge=1, le=4)
    enrolled_at: AwareDatetime | None


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
    lead_fit: LeadEligibility | None


class Formation(WithAllowedActions[FormationAction]):
    event_id: EventId
    phase: FormationPhase
    target: int = Field(ge=1)
    viable: int = Field(ge=1)
    min_size: int = Field(ge=1)
    max_size: int = Field(ge=1)
    core_max: int = Field(ge=2)
    target_override: int | None = Field(ge=1)
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
    name: Name
    description_md: Markdown | None = None


class AdminCreateTeamBody(ReasonBody):
    lead_user_id: UserId
    name: Name


class DeclinePlacementBody(ApiInput):
    withdraw: bool


class TransferLeadBody(ApiInput):
    user_id: UserId


class AddMemberBody(ApiInput):
    user_id: UserId
    area_key: AreaKey | None = None


class AdminAddMemberBody(AddMemberBody):
    reason: Note | None = None


class UpdateMemberBody(ApiInput):
    area_key: AreaKey | None


class CreateSwapBody(ApiInput):
    from_user_id: UserId
    to_user_id: UserId


class Swap(WithAllowedActions[SwapAction]):
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


class Merge(WithAllowedActions[MergeAction]):
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


class UpdateFormationBody(ReasonBody):
    target_override: int | None = Field(default=None, ge=1)
    formation_at_override: AwareDatetime | None = None


class FormationRun(ApiModel):
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


class UpdateReserveOrderBody(ReasonBody):
    user_ids: list[UserId] = Field(min_length=1)


class MoveMemberBody(ReasonBody):
    user_id: UserId
    to_team_id: ProjectId


class AssignLeadBody(ReasonBody):
    user_id: UserId


class UpdateTeamStatusBody(ReasonBody):
    status: TeamStatus
