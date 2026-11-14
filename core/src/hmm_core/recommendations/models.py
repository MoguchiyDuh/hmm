from datetime import datetime
from uuid import UUID

from hmm_contracts.common import RecommendationStatus, RecommendationSubjectType
from pydantic import JsonValue
from sqlalchemy import DateTime, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


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
    enrolled: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    others: Mapped[JsonValue | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    algo_version: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
