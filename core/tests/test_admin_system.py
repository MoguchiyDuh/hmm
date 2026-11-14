from datetime import datetime, timedelta

import pytest
from harness import Harness
from hmm_contracts.common import ActorKind, AlertStatus, AuditAction, AuditEntityType, ErrorCode
from hmm_contracts.notifications import AdminAlertPayload, AlertKind, NotificationType
from hmm_core import models
from hmm_core.admin import service as admin
from hmm_core.admin.schemas import JobStatus, SystemStatus
from hmm_core.notifications.service import alert_contract
from hmm_core.platform.jobs import JobName
from hmm_core.platform.settings import RuntimeSettings
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_api_skeleton import assert_problem

SYSTEM = "/api/v1/admin/system"


def heartbeat(
    run_id: str, started_at: datetime, finished_at: datetime | None
) -> models.ParserHeartbeat:
    return models.ParserHeartbeat(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        stats={"sources_checked": 0, "candidates": 0, "imported": 0, "errors": 0},
        errors=[],
    )


async def add_job(
    session: AsyncSession,
    now: datetime,
    *,
    status: str = "failed",
    task_name: str = "cleanup",
    queue_name: str = "default",
    attempts: int = 5,
    queueing_lock: str | None = None,
) -> int:
    job_id = await session.scalar(
        text(
            "INSERT INTO procrastinate.procrastinate_jobs "
            "(queue_name, task_name, status, attempts, queueing_lock) "
            "VALUES (:queue_name, :task_name, CAST(:status AS procrastinate_job_status), "
            ":attempts, :queueing_lock) RETURNING id"
        ),
        {
            "queue_name": queue_name,
            "task_name": task_name,
            "status": status,
            "attempts": attempts,
            "queueing_lock": queueing_lock,
        },
    )
    assert isinstance(job_id, int)
    await session.execute(
        text(
            "INSERT INTO procrastinate.procrastinate_events (job_id, type, at) "
            "VALUES (:job_id, 'deferred'::procrastinate_job_event_type, :at)"
        ),
        {"at": now - timedelta(hours=2), "job_id": job_id},
    )
    if status == "failed":
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_events (job_id, type, at) "
                "VALUES (:job_id, 'failed'::procrastinate_job_event_type, :at)"
            ),
            {"job_id": job_id, "at": now - timedelta(hours=1)},
        )
    return job_id


async def get_system(h: Harness, actor: models.User) -> SystemStatus:
    async with await h.client(actor) as client:
        response = await client.get(SYSTEM)
    assert response.status_code == 200, response.text
    return SystemStatus.model_validate(response.json())


async def test_system_empty(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    result = await get_system(h, actor)
    assert result.parser_heartbeats == []
    assert result.job_queue_stats == []
    assert result.failed_jobs == []
    assert result.algo_version == "fake-1"


@pytest.mark.parametrize("finished_count", [2, 25])
async def test_system_heartbeats_order_exclusion_and_limit(
    harness: Harness, finished_count: int
) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        session.add_all(
            heartbeat(
                f"run-{index}",
                h.now - timedelta(hours=index + 2),
                h.now - timedelta(minutes=finished_count - index),
            )
            for index in range(finished_count)
        )
        session.add(heartbeat("unfinished", h.now, None))
    result = await get_system(h, actor)
    assert [row.run_id for row in result.parser_heartbeats] == [
        f"run-{index}" for index in range(min(20, finished_count))
    ]
    for row in result.parser_heartbeats:
        assert row.finished_at is not None
        assert row.stats.model_dump() == {
            "sources_checked": 0,
            "candidates": 0,
            "imported": 0,
            "errors": 0,
        }
        assert row.errors == []


async def test_system_queue_stats(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        for status in ["todo", "doing", "failed", "succeeded"]:
            await add_job(session, h.now, status=status)
        await add_job(session, h.now, queue_name="other", status="todo")
    result = await get_system(h, actor)
    assert [row.model_dump() for row in result.job_queue_stats] == [
        {"queue_name": "default", "pending": 1, "running": 1, "failed": 1},
        {"queue_name": "other", "pending": 1, "running": 0, "failed": 0},
    ]


async def test_system_failed_jobs_and_last_error(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        failed_id = await add_job(session, h.now)
        older_id = await add_job(session, h.now - timedelta(days=1), attempts=3)
        await add_job(session, h.now, status="todo")
        await add_job(session, h.now, task_name="unknown_task")
        session.add_all(
            [
                models.AdminAlert(
                    type="job_failing",
                    severity="critical",
                    status="resolved",
                    created_at=h.now - timedelta(hours=1),
                    payload={"job_id": str(failed_id), "error": "boom"},
                ),
                models.AdminAlert(
                    type="job_failing",
                    severity="critical",
                    status="resolved",
                    created_at=h.now - timedelta(hours=2),
                    payload={"job_id": failed_id, "error": "old error"},
                ),
                models.AdminAlert(
                    type="algo_failure",
                    severity="warning",
                    status="open",
                    created_at=h.now,
                    payload={"job_id": str(failed_id), "error": "unrelated"},
                ),
            ]
        )
    result = await get_system(h, actor)
    assert [row.id for row in result.failed_jobs] == [failed_id, older_id]
    failed = result.failed_jobs[0]
    assert failed.task_name == JobName.CLEANUP
    assert failed.queue_name == "default"
    assert failed.status == JobStatus.FAILED
    assert failed.attempts == 5
    assert failed.last_error == "boom"
    assert failed.allowed_actions == ["retry"]
    assert failed.created_at == h.now - timedelta(hours=2)
    assert failed.updated_at == h.now - timedelta(hours=1)
    assert result.failed_jobs[1].attempts == 3
    assert result.failed_jobs[1].last_error is None
    assert result.algo_version == "fake-1"


async def test_system_failed_jobs_limit_and_id_tiebreaker(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        ids = [await add_job(session, h.now) for _ in range(52)]
    result = await get_system(h, actor)
    assert [row.id for row in result.failed_jobs] == list(reversed(ids))[:50]


@pytest.mark.parametrize("reason", [None, "Retry after repair"])
async def test_retry_failed_job_resets_budget_and_audits(
    harness: Harness, reason: str | None
) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        job_id = await add_job(session, h.now, queueing_lock="stale-lock")
        await add_job(session, h.now, status="todo", queueing_lock="stale-lock")
    async with await h.client(actor) as client:
        response = await client.post(
            f"/api/v1/admin/jobs/{job_id}/retry", json={"reason": reason} if reason else None
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["id"] == job_id
        assert job["task_name"] == "cleanup"
        assert job["status"] == "todo"
        assert job["attempts"] == 0
        assert job["allowed_actions"] == []
        assert_problem(
            await client.post(f"/api/v1/admin/jobs/{job_id}/retry"),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.db() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status::text AS status, attempts, scheduled_at, queueing_lock "
                    "FROM procrastinate.procrastinate_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
        assert row.status == "todo"
        assert row.attempts == 0
        assert row.scheduled_at == h.now
        assert row.queueing_lock is None
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id == actor.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.action == AuditAction.JOB_RETRY
        assert audit.entity_type == AuditEntityType.JOB
        assert audit.entity_id == str(job_id)
        assert audit.before == {"status": "failed"}
        assert audit.after == {"status": "todo"}
        assert audit.reason == reason


async def test_retry_rejects_unknown_id_or_task(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        job_id = await add_job(session, h.now, task_name="unknown_task")
    async with await h.client(actor) as client:
        assert_problem(
            await client.post(f"/api/v1/admin/jobs/{job_id}/retry"),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await client.post("/api/v1/admin/jobs/999999/retry"), 404, ErrorCode.NOT_FOUND
        )
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        assert (
            await session.scalar(
                text("SELECT status::text FROM procrastinate.procrastinate_jobs WHERE id = :id"),
                {"id": job_id},
            )
            == "failed"
        )


async def test_system_get_does_not_raise_or_resolve_parser_alerts(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        session.add(heartbeat("stale", h.now - timedelta(hours=28), h.now - timedelta(hours=27)))
    await get_system(h, actor)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 0
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        await admin.check_parser_down(session, h.now, RuntimeSettings())
        session.add(heartbeat("fresh", h.now - timedelta(minutes=1), h.now))
    await get_system(h, actor)
    async with h.db() as session:
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.status == AlertStatus.OPEN
        assert alert.resolved_at is None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_parser_down_deduplicates_notifies_and_recovers(harness: Harness) -> None:
    h = harness
    admins = [await h.admin(), await h.admin()]
    await h.admin(banned_at=h.now)
    await h.user()
    last = h.now - timedelta(hours=27)
    async with h.db() as session:
        session.add(heartbeat("stale", last - timedelta(hours=1), last))
    for _ in range(2):
        async with h.db() as session:
            await admin.check_parser_down(session, h.now, RuntimeSettings())
    async with h.db() as session:
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.type == AlertKind.PARSER_DOWN
        assert alert.status == AlertStatus.OPEN
        assert alert.dedupe_key == "parser_down"
        contract = alert_contract(alert)
        assert contract.title == "Parser is down"
        assert contract.message == f"No parser heartbeat since {last.isoformat()}"
        assert contract.link == "/admin/system"
        assert contract.subject_type == "parser"
        assert contract.subject_title == "Parser"
        notifications = list(await session.scalars(select(models.Notification)))
        assert {row.user_id for row in notifications} == {actor.id for actor in admins}
        assert len(notifications) == 2
        for row in notifications:
            assert row.type == NotificationType.ADMIN_ALERT
            assert AdminAlertPayload.model_validate(row.payload).alert == contract
        session.add(heartbeat("fresh", h.now - timedelta(minutes=1), h.now))
    async with h.db() as session:
        await admin.check_parser_down(session, h.now, RuntimeSettings())
    async with h.db() as session:
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.status == AlertStatus.RESOLVED
        assert alert.resolved_at == h.now
        assert alert.resolved_by is None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


@pytest.mark.parametrize("age_hours", [None, 26, 27])
async def test_parser_down_uses_finished_heartbeat_and_configured_threshold(
    harness: Harness, age_hours: int | None
) -> None:
    h = harness
    await h.admin()
    async with h.db() as session:
        if age_hours is not None:
            last = h.now - timedelta(hours=age_hours)
            session.add(heartbeat("finished", last - timedelta(hours=1), last))
        session.add(heartbeat("unfinished", h.now, None))
    async with h.db() as session:
        await admin.check_parser_down(session, h.now, RuntimeSettings())
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == (
            1 if age_hours == 27 else 0
        )
    async with h.db() as session:
        await admin.check_parser_down(
            session, h.now, RuntimeSettings(parser_heartbeat_max_age_hours=28)
        )
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count()).where(models.AdminAlert.status == AlertStatus.OPEN)
            )
            == 0
        )


async def test_parser_down_without_heartbeats(harness: Harness) -> None:
    async with harness.db() as session:
        await admin.check_parser_down(session, harness.now, RuntimeSettings())
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 0


async def test_system_and_retry_require_web_admin(harness: Harness) -> None:
    h = harness
    member = await h.user()
    actor = await h.admin()
    telegram_id = await h.link_telegram(actor)
    async with h.db() as session:
        job_id = await add_job(session, h.now)
    retry = f"/api/v1/admin/jobs/{job_id}/retry"
    async with await h.client(member) as client:
        assert_problem(await client.get(SYSTEM), 403, ErrorCode.FORBIDDEN)
        assert_problem(await client.post(retry), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as client:
        assert_problem(await client.get(SYSTEM), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await client.post(retry), 401, ErrorCode.UNAUTHENTICATED)
    async with h.bot(telegram_id) as client:
        assert_problem(await client.get(SYSTEM), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await client.post(retry), 401, ErrorCode.UNAUTHENTICATED)
