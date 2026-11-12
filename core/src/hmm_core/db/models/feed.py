from datetime import datetime
from uuid import UUID

from hmm_contracts.common import ConfirmationFactType, ConfirmationSource
from pydantic import JsonValue
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.api.schemas.feed import FeedKind
from hmm_core.db.base import Base, Mutable, enum_check


class Confirmation(Mutable, Base):
    __tablename__ = "confirmations"
    __table_args__ = (
        enum_check("fact_type", ConfirmationFactType),
        enum_check("source", ConfirmationSource),
        Index(
            "uq_confirmations_fact_type_fact_id_confirmer_id_live",
            "fact_type",
            "fact_id",
            "confirmer_id",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("confirmer_id IS NULL OR confirmer_id <> subject_user_id", name="not_self"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    fact_type: Mapped[str] = mapped_column(Text, nullable=False)
    fact_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    subject_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    confirmer_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeedItem(Mutable, Base):
    __tablename__ = "feed_items"
    __table_args__ = (enum_check("kind", FeedKind),)

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    area_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    payload: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
