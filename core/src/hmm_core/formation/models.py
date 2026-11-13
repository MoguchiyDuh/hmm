from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import (
    EnrollmentSource,
    EnrollmentStatus,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
    MergeProposalStatus,
    MergeProposedBy,
    SwapRequestStatus,
)
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


class LeadFit(Mutable, Base):
    __tablename__ = "lead_fits"
    __table_args__ = (
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        CheckConstraint("fit BETWEEN 0 AND 1", name="fit_range"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    fit: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
