from datetime import datetime
from uuid import UUID

from hmm_contracts.common import CvTextStatus, GithubSnapshotStatus
from pydantic import JsonValue
from sqlalchemy import DateTime, ForeignKey, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


class GithubSnapshot(Mutable, Base):
    __tablename__ = "github_snapshots"
    __table_args__ = (enum_check("status", GithubSnapshotStatus),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    repos: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    languages: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    stats: Mapped[JsonValue | None] = mapped_column(JSONB, nullable=True)
    manual_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CvFile(Mutable, Base):
    __tablename__ = "cv_files"
    __table_args__ = (enum_check("text_status", CvTextStatus),)

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_status: Mapped[str] = mapped_column(Text, nullable=False)
    text_error: Mapped[str | None] = mapped_column(Text, nullable=True)
