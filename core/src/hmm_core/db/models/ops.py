from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    ActorKind,
    AlertSeverity,
    AlertStatus,
    AuditAction,
    AuditEntityType,
    DeliveryStatus,
    NotificationChannel,
)
from hmm_contracts.notifications import AlertKind, NotificationType
from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.db.base import Base, Created, Mutable, enum_check
from hmm_core.settings import SettingKey


class Notification(Mutable, Base):
    __tablename__ = "notifications"
    __table_args__ = (enum_check("type", NotificationType),)

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


class AdminAlert(Mutable, Base):
    __tablename__ = "admin_alerts"
    __table_args__ = (
        enum_check("type", AlertKind),
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


class AuditLog(Created, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        enum_check("actor_kind", ActorKind),
        enum_check("action", AuditAction),
        enum_check("entity_type", AuditEntityType),
        CheckConstraint("char_length(reason) <= 1000", name="reason_length"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Mutable, Base):
    __tablename__ = "settings"
    __table_args__ = (enum_check("key", SettingKey),)

    key: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    value: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class ParserHeartbeat(Mutable, Base):
    __tablename__ = "parser_heartbeats"

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    errors: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
