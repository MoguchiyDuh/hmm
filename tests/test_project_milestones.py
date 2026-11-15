from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from factories import new_row, seed_project
from harness import Harness, assert_problem
from sqlalchemy import func, select, update

from hmm import models
from hmm.common import ErrorCode


async def seed_milestone(
    h: Harness,
    project: models.Project,
    *,
    position: int = 0,
    done_by: UUID | None = None,
) -> models.ProjectMilestone:
    async with h.db() as session:
        milestone = models.ProjectMilestone(
            project_id=project.id,
            title="Milestone",
            description=None,
            due_on=None,
            position=position,
            done_at=h.now - timedelta(days=1) if done_by is not None else None,
            done_by=done_by,
        )
        session.add(milestone)
        await session.flush()
        await session.refresh(milestone)
        return milestone


async def test_create_milestone(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member], status="active")
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == project.id)
            .values(joined_at=h.now - timedelta(days=10))
        )
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/milestones",
            json={
                "title": "Ship v1",
                "description": "d",
                "due_on": "2026-12-01",
                "position": 1,
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == str(project.id)
    assert body["title"] == "Ship v1"
    assert body["description"] == "d"
    assert date.fromisoformat(body["due_on"]) == date(2026, 12, 1)
    assert body["position"] == 1
    assert body["done_at"] is None and body["done_by"] is None
    assert body["allowed_actions"] == ["edit", "delete"]
    async with h.db() as session:
        [milestone] = list(await session.scalars(select(models.ProjectMilestone)))
        assert datetime.fromisoformat(body["created_at"]) == milestone.created_at
        row = await session.get(models.Project, project.id)
        assert row is not None and row.last_progress_at == h.now
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_milestone_access_gates(harness: Harness) -> None:
    h = harness
    lead, member, outsider, admin = await h.user(), await h.user(), await h.user(), await h.admin()
    public = await seed_project(h, lead, members=[member])
    private = await seed_project(h, lead, members=[member], visibility="members")
    milestone = await seed_milestone(h, public)
    await seed_milestone(h, private)
    public_path = f"/api/v1/projects/{public.id}/milestones"
    private_path = f"/api/v1/projects/{private.id}/milestones"
    async with await h.client(outsider) as client:
        assert_problem(
            await client.post(public_path, json={"title": "New", "position": 1}),
            403,
            ErrorCode.FORBIDDEN,
        )
        response = await client.get(public_path)
        assert response.status_code == 200, response.text
        assert response.json()[0]["allowed_actions"] == []
        assert_problem(await client.get(private_path), 404, ErrorCode.NOT_FOUND)
        assert_problem(
            await client.post(private_path, json={"title": "New", "position": 1}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.patch(f"{public_path}/{milestone.id}", json={"title": "New"}),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with await h.client(member) as client:
        missing_path = f"/api/v1/projects/{uuid4()}/milestones"
        assert_problem(await client.get(missing_path), 404, ErrorCode.NOT_FOUND)
        assert_problem(
            await client.post(missing_path, json={"title": "New", "position": 1}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.patch(f"{private_path}/{milestone.id}", json={"title": "New"}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.delete(f"{private_path}/{milestone.id}"), 404, ErrorCode.NOT_FOUND
        )
    async with await h.client(admin) as client:
        response = await client.get(private_path)
        assert response.status_code == 200, response.text
        assert response.json()[0]["allowed_actions"] == []
        assert_problem(
            await client.post(private_path, json={"title": "New", "position": 1}),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with h.anonymous() as client:
        assert_problem(await client.get(public_path), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await client.post(public_path, json={"title": "New", "position": 1}),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.user_id == member.id)
            .values(left_at=h.now)
        )
    async with await h.client(member) as client:
        response = await client.get(public_path)
        assert response.status_code == 200 and response.json()[0]["allowed_actions"] == []
        assert_problem(await client.get(private_path), 404, ErrorCode.NOT_FOUND)
        assert_problem(
            await client.post(public_path, json={"title": "New", "position": 1}),
            403,
            ErrorCode.FORBIDDEN,
        )


async def test_milestone_automatic_start(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    idea = await seed_project(h, lead, members=[member])
    async with h.db() as session:
        event = new_row(
            models.Event,
            slug="milestone-team",
            title="Hackathon",
            starts_at=h.now,
            ends_at=h.now + timedelta(days=2),
        )
        session.add(event)
        await session.flush()
        event_id = event.id
    team = await seed_project(h, lead, members=[member], event_id=event_id, team_status="formed")
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        for project in (idea, team):
            path = f"/api/v1/projects/{project.id}"
            response = await client.post(
                f"{path}/milestones", json={"title": "Plan", "position": 0}
            )
            assert response.status_code == 201, response.text
            mid = response.json()["id"]
            response = await client.get(path)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "idea"
            async with h.db() as session:
                row = await session.get(models.Project, project.id)
                assert row is not None and row.last_progress_at == h.now
            h.advance(timedelta(hours=1))
            response = await client.patch(f"{path}/milestones/{mid}", json={"done": True})
            assert response.status_code == 200, response.text
            response = await client.get(path)
            assert response.json()["status"] == ("active" if project.id == idea.id else "idea")
            async with h.db() as session:
                row = await session.get(models.Project, project.id)
                assert row is not None and row.last_progress_at == h.now
                assert row.paused_reason is None


async def test_milestone_done_transitions(harness: Harness) -> None:
    h = harness
    lead, member, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member, other], status="active")
    first = await seed_milestone(h, project)
    second = await seed_milestone(h, project, done_by=other.id)
    path = f"/api/v1/projects/{project.id}/milestones"
    h.advance(timedelta(hours=1))
    done_at = h.now
    async with await h.client(member) as client:
        response = await client.patch(f"{path}/{first.id}", json={"done": True})
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["done_at"]) == done_at
        assert response.json()["done_by"] == str(member.id)
        jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
        assert len(jobs) == 1 and jobs[0]["args"]["user_id"] == str(member.id)
        h.advance(timedelta(hours=1))
        response = await client.patch(f"{path}/{first.id}", json={"done": True})
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["done_at"]) == done_at
        async with h.db() as session:
            row = await session.get(models.Project, project.id)
            assert row is not None and row.last_progress_at == done_at
        assert [
            job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"
        ] == jobs
        response = await client.patch(f"{path}/{second.id}", json={"done": False})
        assert response.status_code == 200, response.text
        assert response.json()["done_at"] is None and response.json()["done_by"] is None
        jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
        assert len(jobs) == 2
        assert {job["args"]["user_id"] for job in jobs} == {str(member.id), str(other.id)}
        async with h.db() as session:
            row = await session.get(models.Project, project.id)
            assert row is not None and row.last_progress_at == h.now


@pytest.mark.parametrize("done", [False, True])
async def test_milestone_field_edit_and_noop(harness: Harness, done: bool) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member])
    milestone = await seed_milestone(h, project, done_by=member.id if done else None)
    path = f"/api/v1/projects/{project.id}/milestones/{milestone.id}"
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        response = await client.patch(path, json={"title": "New title"})
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "New title"
        assert not [
            job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"
        ]
        response = await client.patch(
            path, json={"description": "New", "due_on": "2026-12-01", "position": 5}
        )
        assert response.status_code == 200, response.text
        assert response.json()["position"] == 5
        assert response.json()["description"] == "New"
        assert response.json()["due_on"] == "2026-12-01"
        async with h.db() as session:
            row = await session.get(models.Project, project.id)
            assert row is not None and row.last_progress_at == h.now and row.status == "idea"
            progress_at = row.last_progress_at
            row = await session.get(models.ProjectMilestone, milestone.id)
            assert row is not None
            updated_at = row.updated_at
        h.advance(timedelta(hours=1))
        for body in (
            {},
            {"title": "New title", "position": 5},
            {"description": None, "due_on": None},
        ):
            response = await client.patch(path, json=body)
            assert response.status_code == 200, response.text
            assert datetime.fromisoformat(response.json()["updated_at"]) == updated_at
            assert response.json()["description"] == "New"
            assert response.json()["due_on"] == "2026-12-01"
        async with h.db() as session:
            row = await session.get(models.Project, project.id)
            assert row is not None and row.last_progress_at == progress_at
        assert not [
            job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"
        ]


async def test_delete_milestone(harness: Harness) -> None:
    h = harness
    lead, member, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member, other])
    plain = await seed_milestone(h, project)
    done = await seed_milestone(h, project, done_by=other.id)
    h.advance(timedelta(hours=1))
    path = f"/api/v1/projects/{project.id}/milestones"
    async with await h.client(member) as client:
        response = await client.delete(f"{path}/{plain.id}")
        assert response.status_code == 204, response.text
        assert_problem(await client.delete(f"{path}/{plain.id}"), 404, ErrorCode.NOT_FOUND)
        assert not [
            job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"
        ]
        response = await client.delete(f"{path}/{done.id}")
        assert response.status_code == 204, response.text
    async with h.db() as session:
        assert await session.get(models.ProjectMilestone, plain.id) is None
        assert await session.get(models.ProjectMilestone, done.id) is None
        row = await session.get(models.Project, project.id)
        assert row is not None and row.last_progress_at == h.now and row.status == "idea"
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert len(jobs) == 1 and jobs[0]["args"]["user_id"] == str(other.id)


@pytest.mark.parametrize("action", ["create", "edit", "delete", "done", "undone"])
@pytest.mark.parametrize("cause", ["idle", "manual"])
async def test_milestone_progress_and_pause_resume(
    harness: Harness, action: str, cause: str
) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member], status="paused")
    milestone = await seed_milestone(h, project, done_by=member.id if action == "undone" else None)
    async with h.db() as session:
        row = await session.get(models.Project, project.id)
        assert row is not None
        row.paused_reason = cause
    h.advance(timedelta(hours=1))
    path = f"/api/v1/projects/{project.id}/milestones"
    async with await h.client(member) as client:
        if action == "create":
            response = await client.post(path, json={"title": "New", "position": 1})
        elif action == "delete":
            response = await client.delete(f"{path}/{milestone.id}")
        else:
            body = {"title": "New"} if action == "edit" else {"done": action == "done"}
            response = await client.patch(f"{path}/{milestone.id}", json=body)
        assert response.status_code == {"create": 201, "delete": 204}.get(action, 200), (
            response.text
        )
    async with h.db() as session:
        row = await session.get(models.Project, project.id)
        assert row is not None
        assert row.last_progress_at == h.now
        assert row.status == ("active" if cause == "idle" else "paused")
        assert row.paused_reason == (None if cause == "idle" else "manual")


async def test_closed_project_milestones(harness: Harness) -> None:
    h = harness
    lead = await h.user()
    project = await seed_project(h, lead, status="abandoned")
    milestone = await seed_milestone(h, project)
    path = f"/api/v1/projects/{project.id}/milestones"
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(path, json={"title": "New", "position": 1}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await client.patch(f"{path}/{milestone.id}", json={"title": "New"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(await client.delete(f"{path}/{milestone.id}"), 409, ErrorCode.INVALID_STATE)


async def test_list_milestones_order(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member])
    milestones = [await seed_milestone(h, project, position=position) for position in (3, 1, 2, 2)]
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/projects/{project.id}/milestones")
    assert response.status_code == 200, response.text
    assert [row["position"] for row in response.json()] == [1, 2, 2, 3]
    assert [row["id"] for row in response.json()] == [
        str(milestone.id)
        for milestone in sorted(milestones, key=lambda row: (row.position, row.id))
    ]
    assert all(row["allowed_actions"] == ["edit", "delete"] for row in response.json())
