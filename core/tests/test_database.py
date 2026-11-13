import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, OnboardingStatus
from hmm_core.app import create_app
from hmm_core.models import (
    AdminAlert,
    Area,
    AuditLog,
    Confirmation,
    Enrollment,
    Event,
    Question,
    QuestionOption,
    Quiz,
    Review,
    Setting,
    User,
)
from hmm_core.platform.audit import record_audit
from hmm_core.platform.config import Config
from hmm_core.platform.db import Base, Session, create_sessionmaker
from hmm_core.platform.jobs import RETRY_POLICY, JobName, create_jobs_app, defer_job
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.settings import RuntimeSettings, SettingKey, load_settings, set_setting
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def add_user(session: AsyncSession, github_id: int = 1) -> User:
    user = User(
        github_id=github_id,
        github_login=f"member{github_id}",
        display_name="Member",
        onboarding_status=OnboardingStatus.COMPLETED,
    )
    session.add(user)
    await session.flush()
    return user


async def add_event(session: AsyncSession) -> Event:
    now = datetime.now(UTC)
    event = Event(
        slug="test-event",
        kind="hackathon",
        formation_enabled=True,
        title="Test event",
        summary="Summary",
        description_md="",
        organizer="Organizer",
        starts_at=now + timedelta(days=5),
        ends_at=now + timedelta(days=6),
        timezone="UTC",
        format="online",
        location={},
        language=["en"],
        eligibility="",
        themes=[],
        status="published",
        review_reasons=[],
        origin="manual",
        confidence=1,
        unmapped_areas=[],
    )
    session.add(event)
    await session.flush()
    return event


async def test_migration_roundtrip_and_uuid_defaults(session: AsyncSession) -> None:
    user = await add_user(session)
    assert user.id.version == 7
    assert user.created_at.utcoffset() == timedelta(0)
    assert user.updated_at.utcoffset() == timedelta(0)
    assert len(Base.metadata.tables) == 63
    names = set(
        await session.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    )
    assert set(Base.metadata.tables) <= names
    assert await session.scalar(text("SELECT count(*) FROM settings")) == 0


async def test_check_constraints_match_models(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
        )
    )
    database = {name: definition for name, definition in rows}
    preparer = postgresql.dialect().identifier_preparer
    models = {
        str(preparer.format_constraint(constraint)).strip('"'): str(constraint.sqltext)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert database.keys() == models.keys()
    for name, sql in models.items():
        if name.endswith("_enum"):
            assert set(re.findall(r"'([^']*)'", database[name])) == set(
                re.findall(r"'([^']*)'", sql)
            ), name


async def test_dnd_is_either_until_or_indefinite(session: AsyncSession) -> None:
    user = await add_user(session)
    user.dnd_indefinite = True
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            user.dnd_until = datetime(2026, 12, 1, tzinfo=UTC)
            await session.flush()


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("onboarding_status", "unknown"),
        ("about_md", "x" * 5001),
        ("contacts", {str(i): "x" for i in range(11)}),
        ("contacts", []),
    ],
)
async def test_user_checks(session: AsyncSession, attribute: str, value: Any) -> None:
    user = await add_user(session)
    with pytest.raises(IntegrityError), session.no_autoflush:
        async with session.begin_nested():
            setattr(user, attribute, value)
            await session.flush()


async def test_active_enrollment_partial_unique(session: AsyncSession) -> None:
    user = await add_user(session)
    event = await add_event(session)
    values = dict(event_id=event.id, user_id=user.id, source="self", status_at=datetime.now(UTC))
    session.add(Enrollment(**values, status="active"))
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(Enrollment(**values, status="reserve"))
            await session.flush()
    session.add(Enrollment(**values, status="withdrawn"))
    await session.flush()


async def test_open_alert_partial_unique(session: AsyncSession) -> None:
    values = dict(type="job_failing", severity="critical", payload={}, dedupe_key="test")
    session.add(AdminAlert(**values, status="open"))
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(AdminAlert(**values, status="open"))
            await session.flush()
    session.add(AdminAlert(**values, status="resolved"))
    await session.flush()


async def test_confirmation_nulls_not_distinct(session: AsyncSession) -> None:
    user = await add_user(session)
    values = dict(
        fact_type="attendance",
        fact_id=UUID(int=1),
        subject_user_id=user.id,
        confirmer_id=None,
        source="system",
        ignored=False,
    )
    session.add(Confirmation(**values))
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(Confirmation(**values))
            await session.flush()
    session.add(Confirmation(**values, withdrawn_at=datetime.now(UTC)))
    await session.flush()


async def test_review_nulls_not_distinct(session: AsyncSession) -> None:
    reviewer = await add_user(session)
    reviewee = await add_user(session, 2)
    values = dict(
        subject_type="event",
        subject_id=UUID(int=1),
        window_id=None,
        reviewer_id=reviewer.id,
        reviewee_id=reviewee.id,
        context="host",
        rating=8,
        comment="",
    )
    session.add(Review(**values))
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(Review(**values))
            await session.flush()


async def test_event_array_enum_check(session: AsyncSession) -> None:
    event = await add_event(session)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            event.review_reasons = ["unknown"]
            await session.flush()


async def test_area_key_is_immutable(session: AsyncSession) -> None:
    area = Area(key="backend", name="Backend", description="", sort=0)
    session.add(area)
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            area.key = "backend_new"
            await session.flush()


async def test_question_option_weights(session: AsyncSession) -> None:
    user = await add_user(session)
    quiz = Quiz(version=1, title="Quiz", status="draft", created_by=user.id)
    session.add(quiz)
    await session.flush()
    question = Question(
        quiz_id=quiz.id,
        section="main",
        key="experience",
        type="single_choice",
        text="Experience?",
        required=True,
        position=0,
        config={},
    )
    session.add(question)
    await session.flush()
    option = QuestionOption(
        question_id=question.id,
        key="experienced",
        label="Experienced",
        position=0,
        area_hints={"backend": 1.1},
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(option)
            await session.flush()


async def test_settings_roundtrip_and_audit(session: AsyncSession) -> None:
    actor = await add_user(session)
    assert await load_settings(session) == RuntimeSettings()
    effective = await set_setting(session, SettingKey.REVIEW_HOURS, 60, actor.id, "Longer review")
    assert effective.review_hours == 60
    assert (await load_settings(session)).review_hours == 60
    override = await session.get(Setting, "review_hours")
    assert override is not None and override.updated_by == actor.id
    audit = (await session.scalars(select(AuditLog))).one()
    assert audit.action == AuditAction.SETTING_SET
    assert audit.entity_id == "review_hours"
    assert audit.before == 48 and audit.after == 60
    assert audit.reason == "Longer review"
    await set_setting(session, SettingKey.REVIEW_HOURS, 72, actor.id)
    assert (await load_settings(session)).review_hours == 72
    assert await session.scalar(select(func.count()).select_from(Setting)) == 1


async def test_invalid_setting_writes_nothing(session: AsyncSession) -> None:
    actor = await add_user(session)
    with pytest.raises(ValidationError):
        await set_setting(session, SettingKey.REVIEW_MIN_HOURS, 49, actor.id)
    assert await session.scalar(select(func.count()).select_from(Setting)) == 0
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_settings_and_audit_rollback(session: AsyncSession) -> None:
    actor = await add_user(session)
    await session.commit()
    await set_setting(session, SettingKey.REVIEW_HOURS, 60, actor.id)
    await session.rollback()
    assert await load_settings(session) == RuntimeSettings()
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_record_audit_system_actor(session: AsyncSession) -> None:
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.JOB,
        entity_id="123",
        before={"state": "doing"},
        after={"state": "failed"},
    )
    entry = (await session.scalars(select(AuditLog))).one()
    assert entry.actor_id is None
    assert entry.before == {"state": "doing"}


async def test_worker_runs_test_task(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_jobs_app(migrated_database)
    seen: list[int] = []

    @app.task(name="test_success", retry=RETRY_POLICY)
    async def success(value: int) -> None:
        seen.append(value)

    async with app.open_async():
        await success.defer_async(value=42)
        await app.run_worker_async(wait=False, install_signal_handlers=False)
    assert seen == [42]
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT status::text FROM procrastinate.procrastinate_jobs")
            )
            == "succeeded"
        )


async def test_worker_exhaustion_dedupes_alert(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_jobs_app(migrated_database)

    @app.task(name="test_failure", retry=RETRY_POLICY)
    async def failure() -> None:
        raise RuntimeError("Test failure")

    async with app.open_async():
        await failure.defer_async()
        await failure.defer_async()
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE procrastinate.procrastinate_jobs SET attempts = 5")
            )
        await app.run_worker_async(wait=False, install_signal_handlers=False)
    async with create_sessionmaker(engine)() as session:
        alerts = list(await session.scalars(select(AdminAlert)))
        assert len(alerts) == 1
        assert alerts[0].type == "job_failing"
        assert alerts[0].severity == "critical"
        assert isinstance(alerts[0].payload, dict)
        assert alerts[0].payload["task_name"] == "test_failure"


async def test_worker_retry_before_exhaustion(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_jobs_app(migrated_database)

    @app.task(name="test_retry", retry=RETRY_POLICY)
    async def failure() -> None:
        raise RuntimeError("Retry me")

    async with app.open_async():
        await failure.defer_async()
        await app.run_worker_async(wait=False, install_signal_handlers=False)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status::text, attempts, scheduled_at > now() "
                    "FROM procrastinate.procrastinate_jobs"
                )
            )
        ).one()
        assert row == ("todo", 1, True)
        assert await connection.scalar(text("SELECT count(*) FROM admin_alerts")) == 0


async def test_defer_shares_caller_transaction(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_jobs_app(migrated_database)
    async with create_sessionmaker(engine)() as session:
        await defer_job(session, app, JobName.CLEANUP)
        assert (
            await session.scalar(text("SELECT count(*) FROM procrastinate.procrastinate_jobs")) == 1
        )
        await session.rollback()
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM procrastinate.procrastinate_jobs"))
            == 0
        )
    async with create_sessionmaker(engine).begin() as session:
        await defer_job(session, app, JobName.CLEANUP)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM procrastinate.procrastinate_jobs"))
            == 1
        )


async def test_readyz_database(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_app(Config(public_url="http://test", database_url=migrated_database))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        async with engine.begin() as connection:
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            await connection.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
        try:
            assert (await client.get("/readyz")).status_code == 503
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = :head"), {"head": head}
                )


@pytest.mark.parametrize("descending", [True, False])
async def test_keyset_ties(session: AsyncSession, descending: bool) -> None:
    users = [await add_user(session, github_id=i) for i in range(1, 6)]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    for user in users:
        user.created_at = stamp
    await session.flush()
    ids: list[UUID] = []
    params = CursorParams(limit=2)
    while True:
        page = await keyset_page(
            session,
            select(User),
            params,
            sort=[User.created_at],
            id_column=User.id,
            serialize=lambda row: row.id,
            descending=descending,
        )
        ids.extend(page.items)
        if page.next_cursor is None:
            break
        params = CursorParams(cursor=page.next_cursor, limit=2)
    assert ids == sorted((user.id for user in users), reverse=descending)


async def test_request_transactions(engine: AsyncEngine, migrated_database: str) -> None:
    app = create_app(Config(public_url="http://test", database_url=migrated_database))

    @app.post("/write/{github_id}")
    async def write(github_id: int, session: Session) -> None:
        await add_user(session, github_id)
        if github_id == 2:
            raise RuntimeError("Rollback")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        assert (await client.post("/write/1")).status_code == 200
        assert (await client.post("/write/2")).status_code == 500
    async with AsyncSession(engine) as session:
        assert list(await session.scalars(select(User.github_id))) == [1]
