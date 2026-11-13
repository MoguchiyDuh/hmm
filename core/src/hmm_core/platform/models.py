from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    ActorKind,
)
from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Created, Mutable, enum_check


class AlgoCache(Mutable, Base):
    __tablename__ = "algo_cache"

    namespace: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Created, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        enum_check("actor_kind", ActorKind),
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

    key: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    value: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
