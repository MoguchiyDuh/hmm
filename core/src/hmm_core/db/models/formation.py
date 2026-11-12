from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    ConfirmKind,
    EnrollmentSource,
    EnrollmentStatus,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    SwapRequestStatus,
    TeamStatus,
    Trust,
)
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Mutable, enum_check


class EventFormation(Mutable, Base):
    __tablename__ = "event_formations"
    __table_args__ = (
        enum_check("phase", FormationPhase),
        CheckConstraint("rounds >= 0", name="rounds_nonnegative"),
        CheckConstraint("target_override BETWEEN 1 AND 20", name="target_range"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    target_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    formation_at_override: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    formation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("formation_runs.id", ondelete="SET NULL"), nullable=True
    )


class Enrollment(Mutable, Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        CheckConstraint("char_length(note) <= 1000", name="note_length"),
        enum_check("status", EnrollmentStatus),
        enum_check("source", EnrollmentSource),
        Index(
            "uq_enrollments_event_id_user_id_live",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'placed', 'reserve')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    preferred_area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    willing_to_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    reserve_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Project(Mutable, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("char_length(name) <= 100", name="name_length"),
        CheckConstraint("char_length(description_md) <= 5000", name="description_md_length"),
        CheckConstraint("char_length(goal) <= 1000", name="goal_length"),
        CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10", name="links_length"
        ),
        enum_check("visibility", ProjectVisibility),
        enum_check("status", ProjectStatus),
        enum_check("team_status", TeamStatus),
        enum_check("origin", ProjectOrigin),
        CheckConstraint("team_status IS NULL OR event_id IS NOT NULL", name="team_event"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    links: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    recruiting: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    lead_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    team_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    formed_in_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("formation_runs.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seat_open_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seats_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_lead_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProjectArea(Mutable, Base):
    __tablename__ = "project_areas"

    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False, primary_key=True
    )


class ProjectMember(Mutable, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        enum_check("role", MemberRole),
        enum_check("state", MemberState),
        enum_check("trust", Trust),
        enum_check("joined_via", JoinedVia),
        CheckConstraint("char_length(proposal_reason) <= 1000", name="proposal_reason_length"),
        enum_check("confirm_kind", ConfirmKind),
        enum_check("left_reason", LeftReason),
        Index(
            "uq_project_members_event_id_user_id_live",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL AND is_team_row"),
        ),
        Index(
            "uq_project_members_project_id_live",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'lead' AND left_at IS NULL"),
        ),
        CheckConstraint("NOT is_team_row OR event_id IS NOT NULL", name="team_event"),
        CheckConstraint(
            "state <> 'proposed' OR (confirm_by IS NOT NULL AND confirm_kind IS NOT NULL)",
            name="proposal",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    trust: Mapped[str] = mapped_column(Text, nullable=False)
    joined_via: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirm_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirm_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_team_row: Mapped[bool] = mapped_column(Boolean, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invite(Mutable, Base):
    __tablename__ = "invites"
    __table_args__ = (
        enum_check("kind", InviteKind),
        CheckConstraint("char_length(message) <= 1000", name="message_length"),
        enum_check("status", InviteStatus),
        Index(
            "uq_invites_project_id_user_id_live",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invited_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JoinRequest(Mutable, Base):
    __tablename__ = "join_requests"
    __table_args__ = (
        CheckConstraint("char_length(note) <= 1000", name="note_length"),
        enum_check("status", JoinRequestStatus),
        Index(
            "uq_join_requests_event_id_user_id_live",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_join_requests_project_id_user_id_live",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND event_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeadOffer(Mutable, Base):
    __tablename__ = "lead_offers"
    __table_args__ = (
        enum_check("source", LeadOfferSource),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        enum_check("status", LeadOfferStatus),
        Index(
            "uq_lead_offers_project_id_live",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    offered_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tier: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SwapRequest(Mutable, Base):
    __tablename__ = "swap_requests"
    __table_args__ = (enum_check("status", SwapRequestStatus),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    from_team_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_team_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    requested_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    to_lead_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    from_user_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    to_user_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)


class MergeProposal(Mutable, Base):
    __tablename__ = "merge_proposals"
    __table_args__ = (
        enum_check("proposed_by", MergeProposedBy),
        enum_check("status", MergeProposalStatus),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("formation_runs.id", ondelete="SET NULL"), nullable=True
    )
    absorbed_team_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    target_team_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    proposed_by: Mapped[str] = mapped_column(Text, nullable=False)
    absorbed_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)


class FormationRun(Mutable, Base):
    __tablename__ = "formation_runs"
    __table_args__ = (
        enum_check("kind", FormationKind),
        enum_check("status", FormationRunStatus),
        enum_check("planner", FormationPlanner),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    planner: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    output: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    validation_errors: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
