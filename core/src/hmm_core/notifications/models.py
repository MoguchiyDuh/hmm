from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    AlertSeverity,
    AlertStatus,
    DeliveryStatus,
    NotificationChannel,
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

from hmm_core.platform.db import Base, Mutable, enum_check


class AdminAlert(Mutable, Base):
    __tablename__ = "admin_alerts"
    __table_args__ = (
        enum_check("severity", AlertSeverity),
        enum_check("status", AlertStatus),
        CheckConstraint("char_length(resolution_note) <= 1000", name="resolution_note_length"),
        Index(
            "uq_admin_alerts_dedupe_key_live",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationSetting(Mutable, Base):
    __tablename__ = "notification_settings"

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    web: Mapped[bool] = mapped_column(Boolean, nullable=False)
    telegram: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Notification(Mutable, Base):
    __tablename__ = "notifications"

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationDelivery(Mutable, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        enum_check("channel", NotificationChannel),
        enum_check("status", DeliveryStatus),
    )

    notification_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
