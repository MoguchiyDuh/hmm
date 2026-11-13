from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import (
    HelpOfferStatus,
    HelpRequestFormat,
    HelpRequestStatus,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    Trust,
)
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


class MentorOffer(Mutable, Base):
    __tablename__ = "mentor_offers"

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False, primary_key=True
    )
    topics: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class HelpRequest(Mutable, Base):
    __tablename__ = "help_requests"
    __table_args__ = (
        CheckConstraint("char_length(topic) <= 200", name="topic_length"),
        CheckConstraint("char_length(goal) <= 1000", name="goal_length"),
        enum_check("format", HelpRequestFormat),
        enum_check("status", HelpRequestStatus),
        Index(
            "uq_help_requests_learner_id_area_id_live",
            "learner_id",
            "area_id",
            unique=True,
            postgresql_where=text("status IN ('open', 'direct')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    learner_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    target_mentor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HelpOffer(Mutable, Base):
    __tablename__ = "help_offers"
    __table_args__ = (
        CheckConstraint("char_length(message) <= 1000", name="message_length"),
        enum_check("status", HelpOfferStatus),
        UniqueConstraint("request_id", "mentor_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    request_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("help_requests.id", ondelete="CASCADE"), nullable=False
    )
    mentor_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)


class Mentorship(Mutable, Base):
    __tablename__ = "mentorships"
    __table_args__ = (
        enum_check("kind", MentorshipKind),
        enum_check("status", MentorshipStatus),
        enum_check("outcome", MentorshipOutcome),
        CheckConstraint("char_length(summary) <= 1000", name="summary_length"),
        enum_check("mentor_trust", Trust),
        enum_check("learner_trust", Trust),
        CheckConstraint("mentor_id <> learner_id", name="not_self"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    request_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("help_requests.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    mentor_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    learner_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    mentor_score_at_start: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    learner_score_at_start: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_proposed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    mentor_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    endorsement: Mapped[str | None] = mapped_column(Text, nullable=True)
    mentor_trust: Mapped[str] = mapped_column(Text, nullable=False)
    learner_trust: Mapped[str] = mapped_column(Text, nullable=False)


class MentorshipCheckin(Mutable, Base):
    __tablename__ = "mentorship_checkins"
    __table_args__ = (
        CheckConstraint("char_length(note) <= 1000", name="note_length"),
        UniqueConstraint("mentorship_id", "author_id", "occurred_on"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    mentorship_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("mentorships.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
