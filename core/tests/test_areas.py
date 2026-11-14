import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from harness import Harness
from hmm_contracts.common import (
    SEED_AREA_KEYS,
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
)
from hmm_core import models
from hmm_core.areas import service
from hmm_core.areas.schemas import CreateAreaBody
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from test_api_skeleton import assert_problem

CREATE_BODY = {
    "key": "backend",
    "name": "Backend",
    "description": "Server-side development",
    "sort": 10,
}


def test_seed_migration(migrated_database: str) -> None:
    def migrate(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "DATABASE_URL": migrated_database},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    conninfo = (
        make_url(migrated_database)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    try:
        migrate("downgrade", "0001")
        migrate("upgrade", "head")
        with psycopg.connect(conninfo) as connection:
            rows = connection.execute(
                "SELECT key, name, description, sort FROM areas ORDER BY sort, key"
            ).fetchall()
        assert tuple(row[0] for row in rows) == SEED_AREA_KEYS
        assert [row[1] for row in rows] == [
            "Backend",
            "Frontend",
            "Mobile",
            "ML / AI",
            "Data",
            "Design",
            "Product",
            "DevOps",
            "Security",
            "GameDev",
            "Hardware",
            "QA",
        ]
        assert [row[2] for row in rows] == [
            "APIs, databases, server-side logic, integrations",
            "web UI, client-side apps",
            "iOS, Android, cross-platform apps",
            "machine learning, deep learning, LLM apps, CV/NLP",
            "analytics, BI, data engineering, metrics and unit economics",
            "UI/UX, graphics, branding, pitch visuals",
            "product management, business analysis, scoping, pitching",
            "infrastructure, CI/CD, cloud, deployment",
            "CTF, application and network security",
            "game design and development, engines, graphics",
            "embedded, IoT, robotics, electronics",
            "testing, test automation, quality",
        ]
        assert [row[3] for row in rows] == list(range(10, 121, 10))
        migrate("downgrade", "0001")
        with psycopg.connect(conninfo) as connection:
            assert connection.execute("SELECT count(*) FROM areas").fetchone() == (0,)
    finally:
        migrate("upgrade", "head")


async def make_area(h: Harness, *, key: str = "backend", sort: int = 10) -> models.Area:
    async with h.db() as session:
        area = models.Area(key=key, name=key.title(), description="Original", sort=sort)
        session.add(area)
        await session.flush()
        return area


async def test_create_area_audits_and_enqueues(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with await h.client(admin) as client:
        response = await client.post(
            "/api/v1/admin/areas", json={**CREATE_BODY, "reason": "New catalog"}
        )
        assert response.status_code == 201, response.text
        area = response.json()
        assert {key: area[key] for key in CREATE_BODY} == CREATE_BODY
        assert area["allowed_actions"] == ["edit", "archive"]
        assert area["archived_at"] is None
        assert_problem(
            await client.post("/api/v1/admin/areas", json=CREATE_BODY),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await client.post("/api/v1/admin/areas", json={**CREATE_BODY, "key": "Bad-Key"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    async with h.db() as session:
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.action == AuditAction.AREA_CREATE
        assert audit.entity_type == AuditEntityType.AREA
        assert audit.entity_id == area["id"]
        assert audit.before is None
        assert audit.after == CREATE_BODY
        assert audit.reason == "New catalog"
        assert await session.scalar(select(func.count()).select_from(models.Area)) == 1
    jobs = await h.pending_jobs("recompute_all_members")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {}


async def test_create_area_and_job_roll_back_together(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.sessionmaker() as session:
        area = await service.create_area(
            session, h.jobs, CreateAreaBody.model_validate(CREATE_BODY), admin.id
        )
        assert await session.get(models.Area, area.id) is area
        await session.rollback()
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Area)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert await h.pending_jobs("recompute_all_members") == []


async def test_area_writes_require_admin(harness: Harness) -> None:
    h = harness
    area = await make_area(h)
    member = await h.user()
    for anonymous in (False, True):
        async with h.anonymous() if anonymous else await h.client(member) as client:
            status = 401 if anonymous else 403
            code = ErrorCode.UNAUTHENTICATED if anonymous else ErrorCode.FORBIDDEN
            assert_problem(await client.post("/api/v1/admin/areas", json=CREATE_BODY), status, code)
            assert_problem(
                await client.patch(f"/api/v1/admin/areas/{area.id}", json={"name": "Changed"}),
                status,
                code,
            )
            for action in ("archive", "unarchive"):
                assert_problem(
                    await client.post(f"/api/v1/admin/areas/{area.id}/{action}"), status, code
                )
            if anonymous:
                assert_problem(await client.get("/api/v1/areas"), status, code)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_update_area_changes_only_given_fields(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    area = await make_area(h)
    async with await h.client(admin) as client:
        response = await client.patch(
            f"/api/v1/admin/areas/{area.id}",
            json={"name": "Server", "sort": 0, "description": None, "reason": "Reorder"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Server"
        assert response.json()["sort"] == 0
        assert response.json()["description"] == "Original"
        assert response.json()["key"] == "backend"
        assert_problem(
            await client.patch(f"/api/v1/admin/areas/{area.id}", json={"key": "server"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        for body in ({}, {"name": "Server", "sort": 0}, {"reason": "No changes"}):
            response = await client.patch(f"/api/v1/admin/areas/{area.id}", json=body)
            assert response.status_code == 200, response.text
    async with h.db() as session:
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.action == AuditAction.AREA_EDIT
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.entity_type == AuditEntityType.AREA
        assert audit.entity_id == str(area.id)
        assert audit.before == {"name": "Backend", "sort": 10}
        assert audit.after == {"name": "Server", "sort": 0}
        assert audit.reason == "Reorder"


async def test_archive_preserves_scores_and_unarchive_restores(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    area = await make_area(h)
    async with h.db() as session:
        score = models.AreaScore(
            user_id=member.id, area_id=area.id, calculated=Decimal("7.25"), calc_reason="Existing"
        )
        session.add(score)
        await session.flush()
        score_updated_at = score.updated_at
    async with await h.client(admin) as client, await h.client(member) as member_client:
        assert_problem(
            await client.post(f"/api/v1/admin/areas/{area.id}/unarchive"),
            409,
            ErrorCode.INVALID_STATE,
        )
        response = await client.post(
            f"/api/v1/admin/areas/{area.id}/archive", json={"reason": "Retired"}
        )
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["archived_at"]) == h.now
        assert response.json()["allowed_actions"] == ["edit", "unarchive"]
        assert (await member_client.get("/api/v1/areas")).json() == []
        response = await client.get("/api/v1/areas")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [str(area.id)]
        assert response.json()[0]["allowed_actions"] == ["edit", "unarchive"]
        assert_problem(
            await client.post(f"/api/v1/admin/areas/{area.id}/archive"),
            409,
            ErrorCode.INVALID_STATE,
        )
        response = await client.patch(
            f"/api/v1/admin/areas/{area.id}", json={"description": "Archived but editable"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["description"] == "Archived but editable"
        response = await client.post(f"/api/v1/admin/areas/{area.id}/unarchive")
        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is None
        assert response.json()["allowed_actions"] == ["edit", "archive"]
        response = await member_client.get("/api/v1/areas")
        assert [item["id"] for item in response.json()] == [str(area.id)]
        assert response.json()[0]["allowed_actions"] == []
    async with h.db() as session:
        stored = await session.get(models.AreaScore, (member.id, area.id))
        assert stored is not None
        assert stored.calculated == Decimal("7.25")
        assert stored.calc_reason == "Existing"
        assert stored.updated_at == score_updated_at
        audits = list(
            await session.scalars(
                select(models.AuditLog)
                .where(
                    models.AuditLog.action.in_(
                        [AuditAction.AREA_ARCHIVE, AuditAction.AREA_UNARCHIVE]
                    )
                )
                .order_by(models.AuditLog.id)
            )
        )
        assert [audit.action for audit in audits] == [
            AuditAction.AREA_ARCHIVE,
            AuditAction.AREA_UNARCHIVE,
        ]
        assert audits[0].before == {"archived_at": None}
        assert audits[0].after == {"archived_at": h.now.isoformat()}
        assert audits[0].reason == "Retired"
        assert audits[1].before == audits[0].after
        assert audits[1].after == {"archived_at": None}
        assert audits[1].reason is None
        assert all(audit.actor_id == admin.id for audit in audits)
        assert all(audit.actor_kind == ActorKind.ADMIN for audit in audits)
        assert all(audit.entity_type == AuditEntityType.AREA for audit in audits)
        assert all(audit.entity_id == str(area.id) for audit in audits)
    assert await h.pending_jobs() == []


async def test_area_lists_order_and_actor_actions(harness: Harness) -> None:
    h = harness
    await make_area(h, key="mobile", sort=20)
    await make_area(h, key="frontend", sort=10)
    await make_area(h, key="backend", sort=10)
    for admin in (False, True):
        actor = await h.user(admin=admin)
        async with await h.client(actor) as client:
            response = await client.get("/api/v1/areas")
            assert response.status_code == 200, response.text
            assert [item["key"] for item in response.json()] == ["backend", "frontend", "mobile"]
            assert all(
                item["allowed_actions"] == (["edit", "archive"] if admin else [])
                for item in response.json()
            )


async def test_internal_list_is_active_only_and_parser_only(harness: Harness) -> None:
    h = harness
    area = await make_area(h)
    archived = await make_area(h, key="frontend")
    async with h.db() as session:
        stored = await service.get_area(session, archived.id)
        stored.archived_at = h.now
    async with h.parser() as client:
        response = await client.get("/api/v1/internal/areas")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "items": [
                {
                    "id": str(area.id),
                    "key": "backend",
                    "name": "Backend",
                    "description": "Original",
                    "sort": 10,
                }
            ],
            "next_cursor": None,
        }
    admin = await h.admin()
    async with await h.client(admin) as client:
        assert_problem(await client.get("/api/v1/internal/areas"), 401, ErrorCode.UNAUTHENTICATED)
    async with h.bot() as client:
        assert_problem(await client.get("/api/v1/internal/areas"), 401, ErrorCode.UNAUTHENTICATED)


@pytest.mark.parametrize("action", ["edit", "archive", "unarchive"])
async def test_missing_area_is_not_found(harness: Harness, action: str) -> None:
    h = harness
    admin = await h.admin()
    async with await h.client(admin) as client:
        path = f"/api/v1/admin/areas/{uuid4()}"
        response = (
            await client.patch(path, json={})
            if action == "edit"
            else await client.post(f"{path}/{action}")
        )
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
