from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    PlaceStatus,
    ResultOutcome,
    ReviewContext,
    ReviewReportStatus,
    ReviewSubjectType,
    ReviewWindowSubjectType,
)
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


class ProjectResult(Mutable, Base):
    __tablename__ = "project_results"
    __table_args__ = (
        enum_check("outcome", ResultOutcome),
        enum_check("place_status", PlaceStatus),
        CheckConstraint("char_length(retrospective_md) <= 5000", name="retrospective_md_length"),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)
    award: Mapped[str | None] = mapped_column(Text, nullable=True)
    place_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    place_verified_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    place_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    presentation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrospective_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_areas: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    participants: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    auto_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    submitted_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewWindow(Mutable, Base):
    __tablename__ = "review_windows"
    __table_args__ = (enum_check("subject_type", ReviewWindowSubjectType),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    participants: Mapped[JsonValue] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Review(Mutable, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        enum_check("subject_type", ReviewSubjectType),
        enum_check("context", ReviewContext),
        CheckConstraint("char_length(comment) <= 2000", name="comment_length"),
        CheckConstraint("char_length(removed_reason) <= 1000", name="removed_reason_length"),
        CheckConstraint("rating BETWEEN 1 AND 10", name="rating_range"),
        CheckConstraint("reviewer_id <> reviewee_id", name="not_self"),
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "window_id",
            "reviewer_id",
            "reviewee_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    window_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("review_windows.id", ondelete="RESTRICT"), nullable=True
    )
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reviewer_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewee_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    context: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    removed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReviewReport(Mutable, Base):
    __tablename__ = "review_reports"
    __table_args__ = (
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
        enum_check("status", ReviewReportStatus),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    review_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
