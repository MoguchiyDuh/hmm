from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Mutable(Created):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


def enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(repr(member.value) for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_enum")


def enum_array_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(repr(member.value) for member in enum)
    return CheckConstraint(f"{column} <@ ARRAY[{values}]::text[]", name=f"{column}_enum")
