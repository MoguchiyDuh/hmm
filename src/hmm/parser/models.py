from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm.platform.db import Base, Mutable


class ParserSourceState(Mutable, Base):
    __tablename__ = "parser_source_states"

    source_id: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)
