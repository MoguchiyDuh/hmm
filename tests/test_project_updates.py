from datetime import datetime, timedelta
from uuid import uuid4

from factories import new_row, seed_project
from harness import Harness, assert_problem
from sqlalchemy import func, select, update

from hmm import models
from hmm.common import ErrorCode


async def test_create_update(harness: Harness) -> None:
    h = harness
    lead, member, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member, other], status="active")
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == project.id)
            .values(joined_at=h.now - timedelta(days=10))
        )
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/updates",
            json={"body_md": "Week 1 done", "links": ["https://example.com/a"]},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == str(project.id)
    assert body["author_id"] == str(member.id)
    assert body["body_md"] == "Week 1 done"
    assert body["links"] == ["https://example.com/a"]
    assert body["deleted_at"] is None
    async with h.db() as session:
        row = await session.get(models.Project, project.id)
        assert row is not None and row.last_progress_at == h.now
        [posted] = list(await session.scalars(select(models.ProjectUpdate)))
        assert datetime.fromisoformat(body["created_at"]) == posted.created_at
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert len(jobs) == 1 and jobs[0]["args"]["user_id"] == str(member.id)


async def test_update_validation_and_access_gates(harness: Harness) -> None:
    h = harness
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    public = await seed_project(h, lead, members=[member], status="active")
    private = await seed_project(h, lead, members=[member], visibility="members", status="active")
    abandoned = await seed_project(h, lead, members=[member], status="abandoned")
    h.advance(timedelta(hours=1))
    public_path = f"/api/v1/projects/{public.id}/updates"
    private_path = f"/api/v1/projects/{private.id}/updates"
    async with await h.client(member) as client:
        assert (
            await client.post(public_path, json={"body_md": "x" * 5001, "links": []})
        ).status_code == 422
        assert (
            await client.post(
                public_path, json={"body_md": "x", "links": ["https://example.com"] * 11}
            )
        ).status_code == 422
        assert (
            await client.post(public_path, json={"body_md": "x", "links": ["not a url"]})
        ).status_code == 422
    async with await h.client(outsider) as client:
        assert_problem(
            await client.post(public_path, json={"body_md": "x", "links": []}),
            403,
            ErrorCode.FORBIDDEN,
        )
        assert_problem(await client.get(private_path), 404, ErrorCode.NOT_FOUND)
        assert_problem(
            await client.post(private_path, json={"body_md": "x", "links": []}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.post(
                f"/api/v1/projects/{uuid4()}/updates", json={"body_md": "x", "links": []}
            ),
            404,
            ErrorCode.NOT_FOUND,
        )
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{abandoned.id}/updates", json={"body_md": "x", "links": []}
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.anonymous() as client:
        assert_problem(
            await client.post(public_path, json={"body_md": "x", "links": []}),
            401,
            ErrorCode.UNAUTHENTICATED,
        )


async def test_update_automatic_start_and_resume(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    idea = await seed_project(h, lead, members=[member])
    idle = await seed_project(h, lead, members=[member], status="paused")
    manual = await seed_project(h, lead, members=[member], status="paused")
    async with h.db() as session:
        await session.execute(
            update(models.Project).where(models.Project.id == idle.id).values(paused_reason="idle")
        )
        await session.execute(
            update(models.Project)
            .where(models.Project.id == manual.id)
            .values(paused_reason="manual")
        )
        event = new_row(
            models.Event,
            slug="update-team",
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
        for project in (idea, idle, manual, team):
            response = await client.post(
                f"/api/v1/projects/{project.id}/updates",
                json={"body_md": "Progress", "links": []},
            )
            assert response.status_code == 201, response.text
    async with h.db() as session:
        for project, status, reason in (
            (idea, "active", None),
            (idle, "active", None),
            (manual, "paused", "manual"),
            (team, "idea", None),
        ):
            row = await session.get(models.Project, project.id)
            assert row is not None
            assert row.status == status and row.paused_reason == reason
            assert row.last_progress_at == h.now


async def test_update_recompute_per_author(harness: Harness) -> None:
    h = harness
    lead, member, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member, other], status="active")
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/projects/{project.id}/updates",
                json={"body_md": "Progress", "links": []},
            )
            assert response.status_code == 201, response.text
    async with await h.client(other) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/updates",
            json={"body_md": "Progress", "links": []},
        )
        assert response.status_code == 201, response.text
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ProjectUpdate)) == 3
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert len(jobs) == 2
    assert {job["args"]["user_id"] for job in jobs} == {str(member.id), str(other.id)}


async def test_list_updates(harness: Harness) -> None:
    h = harness
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=[member], status="active")
    async with h.db() as session:
        for hours in (3, 2, 1):
            session.add(
                models.ProjectUpdate(
                    project_id=project.id,
                    author_id=member.id,
                    body_md=f"{hours} hours ago",
                    links=["https://example.com/a"],
                    created_at=h.now - timedelta(hours=hours),
                    deleted_at=None,
                )
            )
        session.add(
            models.ProjectUpdate(
                project_id=project.id,
                author_id=member.id,
                body_md="Deleted",
                links=[],
                created_at=h.now,
                deleted_at=h.now,
            )
        )
    path = f"/api/v1/projects/{project.id}/updates"
    for user in (member, outsider):
        async with await h.client(user) as client:
            response = await client.get(path)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert [row["body_md"] for row in rows] == [
            "1 hours ago",
            "2 hours ago",
            "3 hours ago",
        ]
        assert [datetime.fromisoformat(row["created_at"]) for row in rows] == [
            h.now - timedelta(hours=1),
            h.now - timedelta(hours=2),
            h.now - timedelta(hours=3),
        ]
        assert all(row["author_id"] == str(member.id) for row in rows)
        assert all(row["links"] == ["https://example.com/a"] for row in rows)
