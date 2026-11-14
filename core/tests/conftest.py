import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from harness import Harness, open_harness
from hmm_contracts.common import OnboardingStatus
from hmm_core.models import User
from hmm_core.platform.db import Base, create_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

ROOT = Path(__file__).resolve().parents[2]
TRUNCATE_PUBLIC = """
DO $$
DECLARE
    tables text;
BEGIN
    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ') INTO tables
    FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version';
    IF tables IS NOT NULL THEN
        EXECUTE 'TRUNCATE ' || tables || ' CASCADE';
    END IF;
END
$$
"""


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL tests require TEST_DATABASE_URL")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    first = True
    for args in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head"), ("check",)):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if args == ("upgrade", "head") and first:
            first = False
            with psycopg.connect(conninfo) as connection:
                connection.execute(TRUNCATE_PUBLIC)
        if args == ("downgrade", "base"):
            with psycopg.connect(conninfo) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = 'procrastinate'"
                ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                ).fetchone() == (0,)
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(migrated_database)
    async with engine.begin() as connection:
        tables = ", ".join(f'public."{name}"' for name in Base.metadata.tables)
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
        await connection.execute(
            text("TRUNCATE procrastinate.procrastinate_jobs RESTART IDENTITY CASCADE")
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
        await session.rollback()


@pytest.fixture
def stub_actor() -> User:
    return User(
        id=UUID("019a78e5-9800-7000-8000-000000000001"),
        github_id=1,
        github_login="member",
        display_name="Member",
        is_admin=True,
        onboarding_status=OnboardingStatus.COMPLETED,
    )


@pytest.fixture
async def harness(engine: AsyncEngine, migrated_database: str) -> AsyncIterator[Harness]:
    async with open_harness(engine, migrated_database) as value:
        yield value
