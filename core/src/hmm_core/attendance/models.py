from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    Trust,
)
from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


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
