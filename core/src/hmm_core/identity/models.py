from datetime import datetime
from uuid import UUID

from hmm_contracts.common import Locale, OnboardingStatus
from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hmm_core.platform.db import Base, Mutable, enum_check


class User(Mutable, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(display_name) <= 100", name="display_name_length"),
        CheckConstraint("char_length(about_md) <= 5000", name="about_md_length"),
        enum_check("locale", Locale),
        CheckConstraint("char_length(ban_reason) <= 1000", name="ban_reason_length"),
        enum_check("onboarding_status", OnboardingStatus),
        CheckConstraint(
            (
                "jsonb_typeof(contacts) = 'object' AND "
                "jsonb_array_length(jsonb_path_query_array(contacts, '$.keyvalue()'))"
                " <= 10"
            ),
            name="contacts",
        ),
        CheckConstraint("mentor_capacity >= 0", name="mentor_capacity"),
        CheckConstraint("NOT (dnd_indefinite AND dnd_until IS NOT NULL)", name="dnd"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, server_default=text("uuidv7()"), nullable=False, primary_key=True
    )
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    github_login: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    about_md: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    contacts: Mapped[JsonValue] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    onboarding_status: Mapped[str] = mapped_column(Text, nullable=False)
    dnd_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dnd_indefinite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mentor_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Mutable, Base):
    __tablename__ = "sessions"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class TelegramLink(Mutable, Base):
    __tablename__ = "telegram_links"

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    broken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramLinkCode(Mutable, Base):
    __tablename__ = "telegram_link_codes"

    code: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
