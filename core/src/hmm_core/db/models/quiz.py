from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import QuestionType
from hmm_contracts.common import QuizStatus, SubmissionStatus
from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Mutable, enum_check


class Area(Mutable, Base):
    __tablename__ = "areas"
    __table_args__ = (
        CheckConstraint("char_length(name) <= 100", name="name_length"),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{0,31}$'", name="key_slug"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Quiz(Mutable, Base):
    __tablename__ = "quizzes"
    __table_args__ = (
        CheckConstraint("char_length(title) <= 200", name="title_length"),
        enum_check("status", QuizStatus),
        Index(
            "uq_quizzes_status_live",
            "status",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class Question(Mutable, Base):
    __tablename__ = "questions"
    __table_args__ = (
        enum_check("type", QuestionType),
        UniqueConstraint("quiz_id", "key"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    quiz_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    section: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    help: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    visible_if: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    config: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)


class QuestionOption(Mutable, Base):
    __tablename__ = "question_options"
    __table_args__ = (UniqueConstraint("question_id", "key"),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    question_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    area_hints: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)


class OnboardingSubmission(Mutable, Base):
    __tablename__ = "onboarding_submissions"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(area_hints) = 'object' AND NOT jsonb_path_exists("
            "area_hints, '$.* ? (@.type() != \"number\" || @ < 0 || @ > 1)')",
            name="area_hints_weights",
        ),
        enum_check("status", SubmissionStatus),
        Index(
            "uq_onboarding_submissions_user_id_live",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    quiz_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    answers: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
