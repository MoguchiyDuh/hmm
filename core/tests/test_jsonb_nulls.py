import importlib
from uuid import uuid4

from hmm_core.models import AuditLog, Base
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

migration = importlib.import_module("hmm_core.migrations.versions.0025_jsonb_sql_nulls")


def nullable_jsonb() -> set[tuple[str, str]]:
    return {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, JSONB) and column.nullable
    }


def test_nullable_jsonb_columns_store_sql_null() -> None:
    columns = [
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, JSONB) and column.nullable and not column.type.none_as_null
    ]
    assert columns == []


def test_migration_covers_every_nullable_jsonb_column() -> None:
    assert set(migration.COLUMNS) == nullable_jsonb()


async def test_none_is_sql_null(session: AsyncSession) -> None:
    row = AuditLog(
        actor_kind="system",
        action="user.ban",
        entity_type="user",
        entity_id=str(uuid4()),
        before=None,
        after=None,
    )
    session.add(row)
    await session.flush()
    stored = await session.execute(
        text("SELECT before IS NULL, after IS NULL FROM audit_log WHERE id = :id"), {"id": row.id}
    )
    assert stored.one() == (True, True)
