import fcntl
import hashlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from harness import Harness, open_harness
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from hmm.common import OnboardingStatus
from hmm.models import User
from hmm.platform.db import Base, create_engine

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src/hmm/migrations/versions"
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


def schema_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(VERSIONS.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def conninfo(url: URL, database: str | None = None) -> str:
    url = url.set(drivername="postgresql")
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


def fingerprint_of(admin: psycopg.Connection, database: str) -> str | None:
    row = admin.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
        (database,),
    ).fetchone()
    return None if row is None else row[0]


def stamp(admin: psycopg.Connection, database: str, fingerprint: str) -> None:
    admin.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(database), sql.Literal(fingerprint)
        )
    )


def migrate(url: URL, fingerprint: str) -> None:
    database = url.database or ""
    with psycopg.connect(conninfo(url, "postgres"), autocommit=True) as admin:
        if fingerprint_of(admin, database) == fingerprint:
            return
        with psycopg.connect(conninfo(url)) as connection:
            connection.execute("DROP SCHEMA IF EXISTS procrastinate CASCADE")
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        with psycopg.connect(conninfo(url)) as connection:
            connection.execute(TRUNCATE_PUBLIC)
        stamp(admin, database, fingerprint)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL tests require TEST_DATABASE_URL")
    return url


@pytest.fixture(scope="session")
def migrated_database(
    database_url: str, worker_id: str, tmp_path_factory: pytest.TempPathFactory
) -> str:
    base = make_url(database_url)
    fingerprint = schema_fingerprint()
    if worker_id == "master":
        migrate(base, fingerprint)
        return database_url
    with (tmp_path_factory.getbasetemp().parent / "migrate.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        migrate(base, fingerprint)
    name = f"{base.database}_{worker_id}"
    with psycopg.connect(conninfo(base, "postgres"), autocommit=True) as admin:
        if fingerprint_of(admin, name) != fingerprint:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(base.database or "")
                )
            )
            stamp(admin, name, fingerprint)
    return base.set(database=name).render_as_string(hide_password=False)


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
