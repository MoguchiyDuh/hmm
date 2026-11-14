from datetime import timedelta
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    ActorKind,
    AlertSeverity,
    AlertStatus,
    AuditAction,
    AuditEntityType,
    ErrorCode,
)
from hmm_contracts.notifications import (
    ALERT_SEVERITIES,
    AdminAlertPayload,
    AlertKind,
    NotificationType,
)
from hmm_core import models
from hmm_core.notifications.service import alert_contract, raise_alert
from hmm_core.platform.db import create_sessionmaker
from hmm_core.platform.jobs import RETRY_POLICY, create_jobs_app
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_api_skeleton import assert_problem


@pytest.mark.parametrize("kind", list(AlertKind))
async def test_raise_alert_deduplicates_and_notifies_admins(
    harness: Harness, kind: AlertKind
) -> None:
    h = harness
    admins = [await h.admin(), await h.admin()]
    await h.user()
    await h.admin(banned_at=h.now)
    async with h.db() as session:
        alert = await raise_alert(
            session,
            kind,
            h.now,
            title="Admin attention required",
            message="A system transition needs attention",
            link="/admin/system",
            subject_type="job",
            subject_title="Test task",
            dedupe_key=f"test:{kind}",
        )
        assert alert is not None
        duplicate = await raise_alert(
            session,
            kind,
            h.now,
            title="Admin attention required",
            message="A system transition needs attention",
            link="/admin/system",
            subject_type="job",
            subject_title="Test task",
            dedupe_key=f"test:{kind}",
        )
        assert duplicate is None
        rows = list(await session.scalars(select(models.AdminAlert)))
        assert len(rows) == 1
        assert rows[0].status == AlertStatus.OPEN
        assert rows[0].severity == ALERT_SEVERITIES[kind]
        notifications = list(await session.scalars(select(models.Notification)))
        if ALERT_SEVERITIES[kind] == AlertSeverity.INFO:
            assert notifications == []
        else:
            assert len(notifications) == len(admins)
            assert {row.user_id for row in notifications} == {admin.id for admin in admins}
            for row in notifications:
                assert row.type == NotificationType.ADMIN_ALERT
                payload = AdminAlertPayload.model_validate(row.payload)
                assert payload.alert == alert_contract(alert)


async def test_list_alerts_filters_paginates_and_exposes_actions(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.db() as session:
        info = await raise_alert(
            session,
            AlertKind.FORMATION_UNDERSIZED,
            h.now,
            title="Undersized formation",
            message="Not enough members",
            link="/admin/system",
            subject_type="formation",
        )
        warning = await raise_alert(
            session,
            AlertKind.FORMATION_FAILED,
            h.now,
            title="Formation failed",
            message="Formation needs attention",
            link="/admin/system",
            subject_type="formation",
        )
        resolved = await raise_alert(
            session,
            AlertKind.FORMATION_FAILED,
            h.now,
            title="Previous failure",
            message="Formation recovered",
            link="/admin/system",
            subject_type="formation",
        )
        assert info is not None and warning is not None and resolved is not None
        info.created_at = h.now - timedelta(hours=2)
        warning.created_at = h.now - timedelta(hours=1)
        resolved.created_at = h.now
        resolved.status = AlertStatus.RESOLVED
        resolved.resolved_at = h.now
        expected_ids = [str(resolved.id), str(warning.id), str(info.id)]
    async with await h.client(admin) as client:
        response = await client.get("/api/v1/admin/alerts")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == expected_ids
        assert [item["allowed_actions"] for item in items] == [[], ["resolve"], ["resolve"]]
        assert [item["severity"] for item in items] == ["warning", "warning", "info"]
        cursor = None
        for index, expected_id in enumerate(expected_ids):
            params = {"limit": "1"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get("/api/v1/admin/alerts", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            assert [item["id"] for item in page["items"]] == [expected_id]
            cursor = page["next_cursor"]
            assert (cursor is not None) == (index < len(expected_ids) - 1)
        for filters, filtered_ids in (
            ({"status": "open"}, expected_ids[1:]),
            ({"status": "resolved"}, expected_ids[:1]),
            ({"type": "formation_failed"}, expected_ids[:2]),
            ({"status": "open", "type": "formation_failed"}, expected_ids[1:2]),
        ):
            response = await client.get("/api/v1/admin/alerts", params=filters)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == filtered_ids


async def test_resolve_alert_records_audit_and_is_idempotent(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    another_admin = await h.admin()
    async with h.db() as session:
        alert = await raise_alert(
            session,
            AlertKind.PARSER_DOWN,
            h.now,
            title="Parser down",
            message="Heartbeat missing",
            link="/admin/system",
            subject_type="parser",
        )
        assert alert is not None
        alert_id = alert.id
    url = f"/api/v1/admin/alerts/{alert_id}/resolve"
    async with await h.client(admin) as client:
        missing_note = await client.post(url, json={"reason": "Investigated"})
        assert_problem(missing_note, 422, ErrorCode.VALIDATION_ERROR)
        response = await client.post(
            url, json={"note": "Parser restarted", "reason": "Heartbeat restored"}
        )
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["status"] == "resolved"
        assert first["resolved_by"] == str(admin.id)
        assert first["resolved_at"] == h.now.isoformat().replace("+00:00", "Z")
        assert first["resolution_note"] == "Parser restarted"
        assert first["allowed_actions"] == []
        missing = await client.post(
            f"/api/v1/admin/alerts/{uuid4()}/resolve", json={"note": "Unknown"}
        )
        assert_problem(missing, 404, ErrorCode.NOT_FOUND)
    h.advance(timedelta(hours=1))
    async with await h.client(another_admin) as client:
        response = await client.post(url, json={"note": "Replacement", "reason": "No change"})
        assert response.status_code == 200, response.text
        assert response.json() == first
    async with h.db() as session:
        row = await session.get(models.AdminAlert, alert_id)
        assert row is not None
        assert row.resolved_by == admin.id
        assert row.resolved_at == h.now - timedelta(hours=1)
        assert row.resolution_note == "Parser restarted"
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.action == AuditAction.ALERT_RESOLVE
        assert audit.entity_type == AuditEntityType.ADMIN_ALERT
        assert audit.entity_id == str(alert_id)
        assert audit.before == {"status": "open"}
        assert audit.after == {"status": "resolved"}
        assert audit.reason == "Heartbeat restored"


async def test_admin_alerts_require_web_admin(harness: Harness) -> None:
    h = harness
    member = await h.user()
    admin = await h.admin()
    telegram_id = await h.link_telegram(admin)
    url = f"/api/v1/admin/alerts/{uuid4()}/resolve"
    async with await h.client(member) as client:
        assert_problem(await client.get("/api/v1/admin/alerts"), 403, ErrorCode.FORBIDDEN)
        assert_problem(await client.post(url, json={"note": "Done"}), 403, ErrorCode.FORBIDDEN)
    async with h.bot(telegram_id) as client:
        assert_problem(await client.get("/api/v1/admin/alerts"), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await client.post(url, json={"note": "Done"}), 401, ErrorCode.UNAUTHENTICATED
        )


async def test_job_failure_alert_validates_deduplicates_and_recovers(
    engine: AsyncEngine, migrated_database: str
) -> None:
    app = create_jobs_app(migrated_database)
    failing = True

    @app.task(name="admin_alert_test_task", retry=RETRY_POLICY)
    async def task() -> None:
        if failing:
            raise RuntimeError("Task failure")

    async with app.open_async():
        job_ids = [await task.defer_async(), await task.defer_async()]
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE procrastinate.procrastinate_jobs SET attempts = 5")
            )
        await app.run_worker_async(wait=False, install_signal_handlers=False)
        async with create_sessionmaker(engine).begin() as session:
            alert = (await session.scalars(select(models.AdminAlert))).one()
            contract = alert_contract(alert)
            assert contract.type == AlertKind.JOB_FAILING
            assert contract.severity == AlertSeverity.CRITICAL
            assert contract.subject_type == "job"
            assert contract.subject_title == "admin_alert_test_task"
            assert contract.title == "Job admin_alert_test_task failed"
            assert contract.message == "Task failure"
            assert contract.link == "/admin/system"
            assert contract.status == AlertStatus.OPEN
            assert isinstance(alert.payload, dict)
            assert alert.payload["task_name"] == "admin_alert_test_task"
            assert alert.payload["job_id"] in job_ids
            assert alert.payload["error"] == "Task failure"
            assert alert.dedupe_key is not None
            assert alert.dedupe_key.startswith("job_failing:admin_alert_test_task:")
            alert_id = alert.id
            other = await raise_alert(
                session,
                AlertKind.JOB_FAILING,
                contract.created_at,
                title="Another task failed",
                message="Unrelated failure",
                link="/admin/system",
                subject_type="job",
                extra={"task_name": "another_task"},
            )
            assert other is not None
            other_id = other.id
        failing = False
        await task.defer_async()
        await app.run_worker_async(wait=False, install_signal_handlers=False)
    async with create_sessionmaker(engine)() as session:
        recovered = await session.get(models.AdminAlert, alert_id)
        assert recovered is not None
        assert recovered.status == AlertStatus.RESOLVED
        assert recovered.resolved_at is not None
        assert recovered.resolved_by is None
        assert recovered.resolution_note is None
        assert alert_contract(recovered).status == AlertStatus.RESOLVED
        other = await session.get(models.AdminAlert, other_id)
        assert other is not None
        assert other.status == AlertStatus.OPEN
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
