from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hmm_contracts.common import (
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    EventFormat,
    EventKind,
    EventOrigin,
    EventReviewReason,
    EventRevisionOrigin,
    EventStatus,
    SourcePersonRole,
    Trust,
)
from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Created, Mutable, enum_array_check, enum_check


class Event(Mutable, Base):
    __tablename__ = "events"
    __table_args__ = (
        enum_array_check("review_reasons", EventReviewReason),
        enum_check("kind", EventKind),
        CheckConstraint("char_length(title) <= 200", name="title_length"),
        CheckConstraint("char_length(summary) <= 500", name="summary_length"),
        CheckConstraint("char_length(description_md) <= 20000", name="description_md_length"),
        enum_check("format", EventFormat),
        enum_check("status", EventStatus),
        enum_check("origin", EventOrigin),
        CheckConstraint(
            (
                "(team_size_min IS NULL OR team_size_min BETWEEN 1 AND 20) AND "
                "(team_size_max IS NULL OR team_size_max BETWEEN 1 AND 20) AND "
                "(team_size_min IS NULL OR team_size_max IS NULL OR team_size_min <= "
                "team_size_max)"
            ),
            name="team_size",
        ),
        CheckConstraint("ends_at >= starts_at", name="dates"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("difficulty BETWEEN 1 AND 10", name="difficulty_range"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    formation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    organizer: Mapped[str | None] = mapped_column(Text, nullable=True)
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_opens_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    registration_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    eligibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    themes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    team_size_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_size_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prize: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    difficulty: Mapped[Decimal | None] = mapped_column(Numeric(3, 1), nullable=True)
    difficulty_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    review_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    unmapped_areas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)


class EventArea(Mutable, Base):
    __tablename__ = "event_areas"
    __table_args__ = (enum_check("importance", AreaImportance),)

    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    area_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("areas.id", ondelete="RESTRICT"), nullable=False, primary_key=True
    )
    importance: Mapped[str] = mapped_column(Text, nullable=False)
    headcount: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EventSource(Mutable, Base):
    __tablename__ = "event_sources"

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventSourcePerson(Mutable, Base):
    __tablename__ = "event_source_people"
    __table_args__ = (
        CheckConstraint("char_length(display_name) <= 100", name="display_name_length"),
        enum_check("role", SourcePersonRole),
        Index(
            "uq_event_source_people_identity",
            "source_id",
            text("coalesce(github_login, telegram_username, display_name)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    github_login: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class EventFieldLock(Mutable, Base):
    __tablename__ = "event_field_locks"

    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    field: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    locked_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRevision(Created, Base):
    __tablename__ = "event_revisions"
    __table_args__ = (enum_check("origin", EventRevisionOrigin),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    diff: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    ignored_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)


class EventAttendance(Mutable, Base):
    __tablename__ = "event_attendances"
    __table_args__ = (
        enum_check("status", AttendanceStatus),
        enum_check("role", AttendanceRole),
        CheckConstraint(
            ("jsonb_typeof(proof_links) = 'array' AND jsonb_array_length(proof_links) <= 10"),
            name="proof_links_length",
        ),
        CheckConstraint("char_length(note) <= 1000", name="note_length"),
        enum_check("trust", Trust),
        UniqueConstraint("event_id", "user_id"),
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
    status: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    proof_links: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    trust: Mapped[str] = mapped_column(Text, nullable=False)
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
