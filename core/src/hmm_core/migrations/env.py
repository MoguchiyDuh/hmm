import asyncio
import os

from alembic import context
from hmm_core.models import Base
from hmm_core.platform.db import create_engine
from sqlalchemy import Connection

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    return type_ != "table" or name is None or not name.startswith("procrastinate_")


def migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required for migrations")
    engine = create_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Migrations require a database connection")
else:
    asyncio.run(run())
