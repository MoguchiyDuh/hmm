from collections.abc import AsyncIterator
from functools import cache
from typing import Annotated

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, Request
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hmm_core.db.base import Base

__all__ = [
    "Base",
    "Session",
    "create_engine",
    "create_sessionmaker",
    "get_session",
    "migration_head",
]


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url).set(drivername="postgresql+psycopg")
    return create_async_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})


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
