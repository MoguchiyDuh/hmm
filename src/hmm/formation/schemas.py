from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue

from hmm.algo.pack import Packing
from hmm.common import Contract, EnrollmentStatus, FormationPhase, InviteStatus, Page
from hmm.platform.schemas import ReasonBody


class CreateEnrollmentBody(Contract):
    preferred_area_key: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    willing_to_lead: bool = False


class UpdateEnrollmentBody(Contract):
    preferred_area_key: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    willing_to_lead: bool | None = None


class Enrollment(Contract):
    id: UUID
    event_id: UUID
    user_id: UUID
    party_id: UUID | None
    preferred_area_id: UUID | None
    note: str | None
    willing_to_lead: bool
    status: EnrollmentStatus
    status_at: datetime


class Member(Contract):
    id: UUID
    user_id: UUID
    area_id: UUID | None
    role: str
    state: str
    accept_by: datetime | None


class Team(Contract):
    id: UUID
    name: str
    lead_user_id: UUID | None
    team_status: str | None
    closed: bool
    members: list[Member]


class EnrollmentPage(Page[Enrollment]):
    teams: list[Team] = Field(default_factory=list)


class Invite(Contract):
    id: UUID
    event_id: UUID | None
    party_id: UUID | None
    project_id: UUID | None
    user_id: UUID
    invited_by: UUID | None
    kind: str
    status: InviteStatus
    expires_at: datetime
    responded_at: datetime | None


class Party(Contract):
    id: UUID
    event_id: UUID
    owner_user_id: UUID
    closed: bool
    created_at: datetime
    members: list[Enrollment]
    invites: list[Invite]


class FormationAction(StrEnum):
    ENROLL = "enroll"
    WITHDRAW = "withdraw"
    UPDATE_ENROLLMENT = "update_enrollment"
    CREATE_PARTY = "create_party"
    ACCEPT = "accept"
    DECLINE = "decline"
    LEAVE = "leave"


class Formation(Contract):
    phase: FormationPhase
    target: int
    min: int
    max: int
    formation_at: datetime
    counts: dict[str, int]
    projection: Packing
    my_enrollment: Enrollment | None
    my_team: Team | None
    allowed_actions: list[FormationAction]


class ClosedBody(ReasonBody):
    closed: bool


class InviteBody(ReasonBody):
    user_id: UUID


class TransferLeadBody(ReasonBody):
    user_id: UUID


class UpdateMemberBody(ReasonBody):
    area_key: str | None


class DeclinePlacementBody(ReasonBody):
    withdraw: bool = False


class UpdateFormationBody(ReasonBody):
    target_override: int | None = Field(default=None, ge=1, le=20)
    formation_at_override: AwareDatetime | None = None


class FormationRun(Contract):
    id: UUID
    event_id: UUID
    status: str
    planner: str
    input: JsonValue
    output: JsonValue | None
    validation_errors: JsonValue | None
    algo_version: str
    created_at: datetime
    applied_at: datetime | None
