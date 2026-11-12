from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import (
    ActivityType,
    AreaScoreEventKind,
    FactType,
    FlagStatus,
    FlagSubjectType,
    MemberReviewStatus,
    OverrideEvidence,
    RestrictionKind,
    Severity,
)
from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Created, Mutable, enum_check


class AreaScore(Mutable, Base):
    __tablename__ = "area_scores"
    __table_args__ = (
        CheckConstraint("char_length(override_reason) <= 1000", name="override_reason_length"),
        enum_check("override_evidence", OverrideEvidence),
        CheckConstraint("calculated BETWEEN 0 AND 10", name="calculated_range"),
        CheckConstraint("override BETWEEN 0 AND 10", name="override_range"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False, primary_key=True
    )
    calculated: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    calc_reason: Mapped[str] = mapped_column(Text, nullable=False)
    override: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    override_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemberReview(Mutable, Base):
    __tablename__ = "member_reviews"
    __table_args__ = (
        enum_check("status", MemberReviewStatus),
        CheckConstraint("char_length(admin_note) <= 1000", name="admin_note_length"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    discrepancies: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)


class AreaScoreEvent(Created, Base):
    __tablename__ = "area_score_events"
    __table_args__ = (
        enum_check("kind", AreaScoreEventKind),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    old_effective: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    new_effective: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MemberMetric(Mutable, Base):
    __tablename__ = "member_metrics"

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    xp: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    activity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActivityEntry(Mutable, Base):
    __tablename__ = "activity_entries"
    __table_args__ = (
        enum_check("type", ActivityType),
        CheckConstraint("char_length(title) <= 200", name="title_length"),
        CheckConstraint("char_length(description_md) <= 5000", name="description_md_length"),
        CheckConstraint(
            "jsonb_typeof(links) = 'array' AND jsonb_array_length(links) <= 10", name="links_length"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False)
    links: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    area_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="SET NULL"), nullable=True
    )
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactAward(Mutable, Base):
    __tablename__ = "fact_awards"
    __table_args__ = (
        enum_check("fact_type", FactType),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
    )

    fact_type: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    xp: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    activity: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XpAdjustment(Created, Base):
    __tablename__ = "xp_adjustments"
    __table_args__ = (CheckConstraint("char_length(reason) <= 1000", name="reason_length"),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    admin_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    flag_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("flags.id", ondelete="SET NULL"), nullable=True
    )


class Flag(Mutable, Base):
    __tablename__ = "flags"
    __table_args__ = (
        enum_check("subject_type", FlagSubjectType),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        enum_check("severity", Severity),
        enum_check("status", FlagStatus),
        CheckConstraint("char_length(resolution_note) <= 1000", name="resolution_note_length"),
        Index(
            "uq_flags_subject_type_subject_id_live",
            "subject_type",
            "subject_id",
            unique=True,
            postgresql_where=text("status = 'to_check'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    related_user_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    penalty: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)


class MemberRestriction(Mutable, Base):
    __tablename__ = "member_restrictions"
    __table_args__ = (
        enum_check("kind", RestrictionKind),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    flag_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("flags.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
