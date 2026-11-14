from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import AuditAction, ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


@dataclass
class Queue:
    lead: models.User
    a: models.User
    b: models.User
    admin: models.User
    unverified: UUID
    verified: UUID
    auto: UUID
    other: UUID


def event_row(harness: Harness, slug: str) -> models.Event:
    return models.Event(
        slug=slug,
        kind="hackathon",
        formation_enabled=True,
        title=slug.upper(),
        starts_at=harness.now - timedelta(days=3),
        ends_at=harness.now,
        timezone="UTC",
        language=["en"],
        themes=[],
        status="published",
        review_reasons=[],
        origin="manual",
        unmapped_areas=[],
    )


@pytest.fixture
async def queue(harness: Harness) -> Queue:
    await harness.seed_areas()
    lead = await harness.user(login="lead")
    a = await harness.user(login="alice")
    b = await harness.user(login="bob")
    outsider = await harness.user(login="outsider")
    admin = await harness.admin()
    async with harness.db() as session:
        event = event_row(harness, "nl-2026")
        other_event = event_row(harness, "other-2026")
        session.add_all([event, other_event])
        await session.flush()
        projects: list[models.Project] = []
        specs: list[tuple[models.Event, dict[str, Any]]] = [
            (other_event, {"place": 1, "award": "Gold", "place_status": "unverified"}),
            (event, {"place": 2, "award": "2nd place", "place_status": "unverified"}),
            (event, {"place": 3, "award": None, "place_status": "verified"}),
            (event, {"place": None, "award": None, "place_status": None, "auto_closed": True}),
        ]
        for index, (owner, fields) in enumerate(specs):
            project = models.Project(
                name=f"Project {index}",
                description_md="",
                goal="",
                links=[],
                visibility="public",
                status="shipped",
                team_status="finished",
                origin="claim",
                event_id=owner.id,
                lead_user_id=lead.id,
            )
            session.add(project)
            await session.flush()
            projects.append(project)
            session.add(
                models.ProjectResult(
                    project_id=project.id,
                    outcome="finished",
                    retrospective_md=f"retro {index}",
                    member_areas={},
                    participants={},
                    submitted_by=lead.id,
                    submitted_at=harness.now,
                    **{"auto_closed": False, **fields},
                )
            )
        target = projects[1]
        for user, role in ((lead, "lead"), (a, "member"), (b, "member")):
            session.add(
                models.ProjectMember(
                    project_id=target.id,
                    event_id=event.id,
                    user_id=user.id,
                    role=role,
                    state="confirmed",
                    trust="self",
                    joined_via="formation",
                    is_team_row=True,
                    joined_at=harness.now - timedelta(days=2),
                )
            )
        session.add_all(
            [
                models.EventSourcePerson(
                    event_id=event.id,
                    source_id="devpost:7",
                    github_login="alice",
                    display_name="Alex",
                    role="winner",
                    place=1,
                    matched_user_id=a.id,
                ),
                models.EventSourcePerson(
                    event_id=event.id,
                    source_id="devpost:7",
                    github_login="stranger",
                    display_name="Stranger",
                    role="winner",
                    place=3,
                    matched_user_id=None,
                ),
                models.EventSourcePerson(
                    event_id=event.id,
                    source_id="devpost:7",
                    github_login="outsider",
                    display_name="Outsider",
                    role="winner",
                    place=2,
                    matched_user_id=outsider.id,
                ),
            ]
        )
    await harness.run_jobs()
    return Queue(lead, a, b, admin, projects[1].id, projects[2].id, projects[3].id, projects[0].id)


async def audits(harness: Harness, action: AuditAction) -> list[models.AuditLog]:
    async with harness.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def notification_count(harness: Harness) -> int:
    async with harness.db() as session:
        return await session.scalar(select(func.count()).select_from(models.Notification)) or 0


async def todo_jobs(harness: Harness) -> list[str]:
    return sorted(
        job["args"]["user_id"]
        for job in await harness.pending_jobs("recompute_member")
        if job["status"] == "todo"
    )


async def decide(harness: Harness, user: models.User, project_id: UUID, **body: Any) -> Any:
    async with await harness.client(user) as c:
        return await c.post(f"/api/v1/admin/projects/{project_id}/result/verify", json=body)


async def test_queue_filters(harness: Harness, queue: Queue) -> None:
    async with await harness.client(queue.admin) as c:
        response = await c.get("/api/v1/admin/results", params={"place_status": "unverified"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["project_id"] for item in items] == [str(queue.unverified), str(queue.other)]
        item = items[0]
        assert item["project"]["id"] == str(queue.unverified)
        assert item["project"]["name"] == "Project 1"
        assert item["project"]["team_status"] == "finished"
        assert item["retrospective_md"] == "retro 1"
        assert item["place"] == 2
        assert item["allowed_actions"] == ["edit", "verify"]
        assert [
            (row["source_id"], row["place"], row["matched_user_id"])
            for row in item["winner_candidates"]
        ] == [("devpost:7", 1, str(queue.a.id))]
        assert items[1]["winner_candidates"] == []
        response = await c.get("/api/v1/admin/results", params={"place_status": "verified"})
        assert [item["project_id"] for item in response.json()["items"]] == [str(queue.verified)]
        response = await c.get("/api/v1/admin/results")
        assert [item["project_id"] for item in response.json()["items"]] == [
            str(queue.auto),
            str(queue.verified),
            str(queue.unverified),
            str(queue.other),
        ]
        assert response.json()["next_cursor"] is None


async def test_queue_pagination(harness: Harness, queue: Queue) -> None:
    async with await harness.client(queue.admin) as c:
        first = (await c.get("/api/v1/admin/results", params={"limit": 1})).json()
        assert [item["project_id"] for item in first["items"]] == [str(queue.auto)]
        assert first["next_cursor"] is not None
        second = (
            await c.get(
                "/api/v1/admin/results", params={"limit": 1, "cursor": first["next_cursor"]}
            )
        ).json()
        assert [item["project_id"] for item in second["items"]] == [str(queue.verified)]
        assert_problem(
            await c.get("/api/v1/admin/results", params={"cursor": "garbage"}),
            400,
            ErrorCode.BAD_REQUEST,
        )


async def test_verify_and_repeat(harness: Harness, queue: Queue) -> None:
    notifications = await notification_count(harness)
    response = await decide(
        harness, queue.admin, queue.unverified, decision="verify", note="checked devpost"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["place_status"] == "verified"
    assert data["place_verified_by"] == str(queue.admin.id)
    assert data["allowed_actions"] == ["edit"]
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, queue.unverified)
        assert result is not None
        assert result.place_verified_at == harness.now
    [audit] = await audits(harness, AuditAction.RESULT_VERIFY)
    assert audit.reason == "checked devpost"
    assert audit.entity_id == str(queue.unverified)
    assert isinstance(audit.before, dict)
    assert isinstance(audit.after, dict)
    assert audit.before["place_status"] == "unverified"
    assert audit.after["place_status"] == "verified"
    expected = sorted(str(user.id) for user in (queue.lead, queue.a, queue.b))
    assert await todo_jobs(harness) == expected
    assert await notification_count(harness) == notifications
    jobs = await harness.pending_jobs("recompute_member")
    repeat = await decide(
        harness, queue.admin, queue.unverified, decision="verify", note="checked devpost"
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json() == data
    assert len(await audits(harness, AuditAction.RESULT_VERIFY)) == 1
    assert await harness.pending_jobs("recompute_member") == jobs
    response = await decide(harness, queue.admin, queue.unverified, decision="reject")
    assert response.status_code == 200, response.text
    assert response.json()["place_status"] == "rejected"
    [reject] = await audits(harness, AuditAction.RESULT_REJECT)
    assert reject.reason is None


async def test_reject_unverified(harness: Harness, queue: Queue) -> None:
    response = await decide(harness, queue.admin, queue.unverified, decision="reject")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["place_status"] == "rejected"
    assert data["place_verified_by"] == str(queue.admin.id)
    assert "verify" not in data["allowed_actions"]
    assert len(await audits(harness, AuditAction.RESULT_REJECT)) == 1


async def test_verify_errors(harness: Harness, queue: Queue) -> None:
    assert_problem(
        await decide(harness, queue.admin, queue.auto, decision="verify"),
        409,
        ErrorCode.INVALID_STATE,
    )
    assert_problem(
        await decide(harness, queue.admin, uuid4(), decision="verify"), 404, ErrorCode.NOT_FOUND
    )
    assert_problem(
        await decide(harness, queue.a, queue.unverified, decision="verify"),
        403,
        ErrorCode.FORBIDDEN,
    )
    async with await harness.client(queue.a) as c:
        assert_problem(await c.get("/api/v1/admin/results"), 403, ErrorCode.FORBIDDEN)
    assert_problem(
        await decide(harness, queue.admin, queue.unverified, decision="maybe"),
        422,
        ErrorCode.VALIDATION_ERROR,
    )
