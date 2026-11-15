from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    EnrollmentSource,
    EnrollmentStatus,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
)
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Created, Mutable, enum_check


class EventFormation(Mutable, Base):
    __tablename__ = "event_formations"
    __table_args__ = (
        enum_check("phase", FormationPhase),
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
    party_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("parties.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    willing_to_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Party(Created, Base):
    __tablename__ = "parties"

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class FormationRun(Mutable, Base):
    __tablename__ = "formation_runs"
    __table_args__ = (
        enum_check("status", FormationRunStatus),
        enum_check("planner", FormationPlanner),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    planner: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    output: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    validation_errors: Mapped[JsonValue | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
