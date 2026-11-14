import asyncio
from datetime import timedelta

import pytest
from harness import Harness
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts.algo import LLMConfig
from hmm_contracts.common import DeliveryStatus, NotificationChannel
from hmm_contracts.fixtures import load_events
from hmm_core.models import (
    AlgoCache,
    Notification,
    NotificationDelivery,
    TelegramLinkCode,
    UserSession,
)
from hmm_core.platform.algo import (
    ALGO_TIMEOUTS,
    CACHE_FOREVER,
    AlgoUnavailableError,
    DatabaseAlgoCache,
    call_algo,
    load_algorithm,
)
from hmm_core.platform.config import Config
from hmm_core.platform.jobs import (
    JOBS,
    JobName,
    JobSpec,
    Runtime,
    bind,
    create_jobs_app,
    defer_job,
    register_jobs,
)
from hmm_core.platform.maintenance import cleanup
from sqlalchemy import select, text


async def test_registered_task_runs_with_fake_algorithm(
    harness: Harness, migrated_database: str
) -> None:
    app = create_jobs_app(migrated_database)
    seen: list[str] = []
    event = next(iter(load_events().values()))

    async def body(runtime: Runtime, user_id: str) -> None:
        assert isinstance(runtime.algorithm, FakeAlgorithm)
        async with runtime.sessionmaker.begin() as session:
            ctx = await runtime.algo_context(session)
            await call_algo("lead_fit", runtime.algorithm.lead_fit(ctx, event, []))
        seen.append(user_id)

    bind(app, JobName.SCORE_ONBOARDING, body)
    async with app.open_async():
        async with harness.db() as session:
            await defer_job(session, app, JobName.SCORE_ONBOARDING, user_id="u1")
        await app.run_worker_async(
            wait=False,
            install_signal_handlers=False,
            additional_context={"runtime": harness.runtime},
        )
    assert seen == ["u1"]
    rows = await harness.pending_jobs("score_onboarding")
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"


async def test_defer_applies_locks(harness: Harness) -> None:
    async with harness.db() as session:
        first = await defer_job(session, harness.jobs, JobName.GITHUB_SYNC, user_id="a")
        duplicate = await defer_job(session, harness.jobs, JobName.GITHUB_SYNC, user_id="a")
        assert duplicate is None
        assert await session.scalar(text("SELECT 1")) == 1
        other = await defer_job(session, harness.jobs, JobName.GITHUB_SYNC, user_id="b")
        assert first is not None
        assert other is not None
    rows = await harness.pending_jobs("github_sync")
    assert len(rows) == 2
    assert {(row["lock"], row["queueing_lock"]) for row in rows} == {
        ("user:a", "github_sync:a"),
        ("user:b", "github_sync:b"),
    }


async def test_run_formation_lock_is_dynamic(harness: Harness) -> None:
    async with harness.db() as session:
        first = await defer_job(session, harness.jobs, JobName.RUN_FORMATION, event_id="e1")
        second = await defer_job(session, harness.jobs, JobName.RUN_FORMATION, event_id="e1")
        assert first is not None
        assert second is not None
        assert first != second
    rows = await harness.pending_jobs("run_formation")
    assert len(rows) == 2
    assert all(row["lock"] == "formation:e1" for row in rows)
    assert all(row["queueing_lock"] is None for row in rows)


def test_schedules(migrated_database: str) -> None:
    assert {name: spec.cron for name, spec in JOBS.items() if spec.cron} == {
        "github_sync_all": "0 3 * * *",
        "recompute_all_members": "0 4 * * *",
        "compute_lead_fits": "0 5 * * *",
        "rank_reserve": "30 5 * * *",
        "scan_community": "0 2 * * *",
        "community_tick": "*/15 * * * *",
        "event_tick": "*/5 * * * *",
        "admin_digest": "0 9 * * 1",
        "archive_old_events": "30 1 * * *",
        "cleanup": "0 1 * * *",
    }
    app = register_jobs(create_jobs_app(migrated_database))
    periodic = app.periodic_registry.periodic_tasks[("cleanup", "cleanup")]
    assert periodic.task.name == "cleanup"
    assert periodic.cron == "0 1 * * *"


async def test_job_timeout_is_retried(
    harness: Harness, migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(JOBS, JobName.CLEANUP, JobSpec(timeout=0.05))
    app = create_jobs_app(migrated_database)

    async def body(runtime: Runtime) -> None:
        await asyncio.sleep(1)

    bind(app, JobName.CLEANUP, body)
    async with app.open_async():
        async with harness.db() as session:
            await defer_job(session, app, JobName.CLEANUP)
        await app.run_worker_async(
            wait=False,
            install_signal_handlers=False,
            additional_context={"runtime": harness.runtime},
        )
    async with harness.db() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status::text, attempts, scheduled_at > now() "
                    "FROM procrastinate.procrastinate_jobs WHERE task_name = 'cleanup'"
                )
            )
        ).one()
        assert row == ("todo", 1, True)
        assert await session.scalar(text("SELECT count(*) FROM admin_alerts")) == 0


async def test_call_algo_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ALGO_TIMEOUTS, "recommend", 0.01)
    with pytest.raises(TimeoutError):
        await call_algo("recommend", asyncio.sleep(1))


def test_load_algorithm() -> None:
    assert isinstance(
        load_algorithm(Config(public_url="http://test", algorithm="fake")), FakeAlgorithm
    )
    for algorithm in (None, "other"):
        with pytest.raises(AlgoUnavailableError):
            load_algorithm(Config(public_url="http://test", algorithm=algorithm))


def test_config_llm() -> None:
    config = Config(public_url="http://test", llm_base_url="u", llm_api_key="k", llm_model="m")
    llm = config.llm()
    assert isinstance(llm, LLMConfig)
    assert llm.base_url == "u"
    assert llm.api_key.get_secret_value() == "k"
    assert llm.model == "m"
    assert Config(public_url="http://test").llm() is None
    for field in ("llm_base_url", "llm_api_key", "llm_model"):
        assert config.model_copy(update={field: None}).llm() is None
    from_env = Config.from_env(
        {"ALGORITHM": "fake", "LLM_BASE_URL": "u", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
    )
    assert from_env.algorithm == "fake"
    assert from_env.llm_base_url == "u"
    assert from_env.llm_api_key == "k"
    assert from_env.llm_model == "m"
    assert from_env.llm() == llm


async def test_algo_cache(harness: Harness) -> None:
    cache = DatabaseAlgoCache(harness.sessionmaker, harness.clock)
    assert await cache.get("primary", "key") is None
    await cache.set("primary", "key", b"first", ttl=timedelta(minutes=5))
    assert await cache.get("primary", "key") == b"first"
    await cache.set("primary", "key", b"second", ttl=timedelta(minutes=5))
    assert await cache.get("primary", "key") == b"second"
    harness.advance(timedelta(minutes=6))
    assert await cache.get("primary", "key") is None
    now = harness.now
    await cache.set("primary", "key", b"forever", ttl=None)
    await cache.set("other", "key", b"untouched", ttl=None)
    async with harness.db() as session:
        row = (
            await session.scalars(
                select(AlgoCache).where(AlgoCache.namespace == "primary", AlgoCache.key == "key")
            )
        ).one()
        assert row.expires_at == now + CACHE_FOREVER
    assert await cache.get("primary", "key") == b"forever"
    await cache.delete("primary", "key")
    assert await cache.get("primary", "key") is None
    async with harness.db() as session:
        assert await session.get(AlgoCache, ("primary", "key")) is None
    assert await cache.get("other", "key") == b"untouched"


async def test_cleanup(harness: Harness) -> None:
    user = await harness.user()
    now = harness.now
    expired = now - timedelta(days=1)
    live = now + timedelta(days=1)
    notifications = [
        Notification(user_id=user.id, type=f"cleanup_{index}", payload={}) for index in range(4)
    ]
    async with harness.db() as session:
        session.add_all(
            [
                UserSession(
                    token_hash=b"expired",
                    user_id=user.id,
                    expires_at=expired,
                    last_used_at=expired,
                ),
                UserSession(
                    token_hash=b"live",
                    user_id=user.id,
                    expires_at=live,
                    last_used_at=now,
                ),
                TelegramLinkCode(code="expired", user_id=user.id, expires_at=expired),
                TelegramLinkCode(code="used", user_id=user.id, expires_at=live, used_at=expired),
                TelegramLinkCode(code="live", user_id=user.id, expires_at=live),
                AlgoCache(namespace="cleanup", key="expired", value=b"expired", expires_at=expired),
                AlgoCache(namespace="cleanup", key="live", value=b"live", expires_at=live),
                *notifications,
            ]
        )
        await session.flush()
        for notification, (status, age) in zip(
            notifications,
            (
                (DeliveryStatus.SENT, 31),
                (DeliveryStatus.FAILED, 31),
                (DeliveryStatus.PENDING, 31),
                (DeliveryStatus.SENT, 1),
            ),
            strict=True,
        ):
            session.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    channel=NotificationChannel.TELEGRAM,
                    status=status,
                    attempts=0 if status == DeliveryStatus.PENDING else 1,
                    next_attempt_at=now,
                    updated_at=now - timedelta(days=age),
                )
            )
    for _ in range(2):
        await cleanup(harness.runtime)
        async with harness.db() as session:
            assert list(await session.scalars(select(UserSession.token_hash))) == [b"live"]
            assert list(await session.scalars(select(TelegramLinkCode.code))) == ["live"]
            deliveries = list(await session.scalars(select(NotificationDelivery)))
            assert {
                (row.notification_id, row.channel, row.status, row.updated_at) for row in deliveries
            } == {
                (
                    notifications[2].id,
                    NotificationChannel.TELEGRAM,
                    DeliveryStatus.PENDING,
                    now - timedelta(days=31),
                ),
                (
                    notifications[3].id,
                    NotificationChannel.TELEGRAM,
                    DeliveryStatus.SENT,
                    now - timedelta(days=1),
                ),
            }
            caches = list(await session.scalars(select(AlgoCache)))
            assert [(row.namespace, row.key, row.value, row.expires_at) for row in caches] == [
                ("cleanup", "live", b"live", live)
            ]
