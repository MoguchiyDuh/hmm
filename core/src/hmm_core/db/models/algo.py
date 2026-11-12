from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import RecommendationStatus, RecommendationSubjectType
from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, Numeric, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Mutable, enum_check


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


class RecommendationSet(Mutable, Base):
    __tablename__ = "recommendation_sets"
    __table_args__ = (
        enum_check("subject_type", RecommendationSubjectType),
        enum_check("status", RecommendationStatus),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrolled: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    others: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlgoCache(Mutable, Base):
    __tablename__ = "algo_cache"

    namespace: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
