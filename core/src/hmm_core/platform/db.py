from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from functools import cache
from typing import Annotated

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, Request
from sqlalchemy import CheckConstraint, DateTime, MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
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


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url).set(drivername="postgresql+psycopg")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3, "options": "-c search_path=public,procrastinate"},
    )


@cache
def migration_head() -> str | None:
    config = Config()
    config.set_main_option("script_location", "hmm_core:migrations")
    return ScriptDirectory.from_config(config).get_current_head()


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = request.app.state.sessionmaker
    if factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with factory.begin() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
