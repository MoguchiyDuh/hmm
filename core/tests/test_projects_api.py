import itertools
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import AreaImportance, ErrorCode, EventKind, EventOrigin, EventStatus
from hmm_core import models
from hmm_core.catalog import service as catalog
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)

BODY: dict[str, Any] = {
    "name": "Chess engine",
    "description_md": "d",
    "goal": "g",
    "area_keys": ["backend", "ml_ai"],
    "links": ["https://example.com/x"],
}


async def make_hackathon(h: Harness, *, starts_in: timedelta = timedelta(days=30)) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                team_size_min=None,
                team_size_max=None,
                registration_deadline=None,
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def seed_project(
    h: Harness,
    lead: models.User,
    *,
    members: Sequence[models.User] = (),
    name: str = "Project",
    visibility: str = "public",
    status: str = "idea",
    event_id: Any = None,
    area_keys: Sequence[str] = ("backend",),
    recruiting: dict[str, Any] | None = None,
    team_status: str | None = None,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event_id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=recruiting,
            lead_user_id=lead.id,
            status=status,
            team_status=team_status,
            origin="standalone" if team_status is None else "claim",
            last_lead_action_at=h.now,
            last_progress_at=h.now,
        )
        session.add(project)
        await session.flush()
        for key in area_keys:
            area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
            assert area_id is not None
            session.add(models.ProjectArea(project_id=project.id, area_id=area_id))
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event_id if team_status is not None else None,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="create",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def test_create_project(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    async with await h.client(a) as c:
        response = await c.post("/api/v1/projects", json=BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["view"] == "full"
    assert body["status"] == "idea"
    assert body["team_status"] is None
    assert body["visibility"] == "public"
    assert body["lead_user_id"] == str(a.id)
    assert body["area_keys"] == ["backend", "ml_ai"]
    assert body["open_seats"] is None
    assert body["recruiting"] is None
    assert body["links"] == ["https://example.com/x"]
    [member] = body["members"]
    assert member["role"] == "lead"
    assert member["joined_via"] == "create"
    assert member["state"] == "confirmed"
    assert member["is_team_row"] is False
    async with h.db() as session:
        project = await session.get(models.Project, body["id"])
        assert project is not None
        assert project.origin == "standalone"
        assert project.last_lead_action_at == h.now
        assert project.last_progress_at == h.now
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectArea)
                .where(models.ProjectArea.project_id == project.id)
            )
            == 2
        )
        audits = list(await session.scalars(select(models.AuditLog)))
        assert len(audits) == 1
        assert audits[0].action == "project_create"
        assert audits[0].actor_id == a.id
        assert audits[0].actor_kind == "user"
        assert audits[0].entity_id == str(project.id)
        assert await session.scalar(select(func.count()).select_from(models.FeedItem)) == 0
    jobs = [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]
    assert len(jobs) == 1
    assert jobs[0]["args"]["project_id"] == body["id"]


async def test_create_project_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    admin = await h.admin()
    event = await make_hackathon(h)
    async with h.db() as session:
        session.add(
            models.MemberRestriction(
                user_id=a.id,
                kind="creating_projects",
                until=None,
                reason="spam",
                created_by=admin.id,
            )
        )
    async with await h.client(a) as c:
        assert_problem(await c.post("/api/v1/projects", json=BODY), 403, ErrorCode.RESTRICTED)
    async with await h.client(b) as c:
        assert_problem(
            await c.post("/api/v1/projects", json={**BODY, "area_keys": ["nope"]}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(
            await c.post("/api/v1/projects", json={**BODY, "area_keys": ["backend", "backend"]}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(
            await c.post("/api/v1/projects", json={**BODY, "event_id": str(uuid4())}),
            404,
            ErrorCode.NOT_FOUND,
        )
        response = await c.post("/api/v1/projects", json={**BODY, "event_id": str(event.id)})
    async with h.anonymous() as c:
        assert_problem(await c.post("/api/v1/projects", json=BODY), 401, ErrorCode.UNAUTHENTICATED)
    assert response.status_code == 201, response.text
    assert response.json()["event_id"] == str(event.id)
    assert response.json()["team_status"] is None
    assert response.json()["origin"] == "standalone"


async def test_create_project_lead_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    projects = [await seed_project(h, a, name=f"P{index}") for index in range(5)]
    async with await h.client(a) as c:
        assert_problem(
            await c.post("/api/v1/projects", json=BODY), 409, ErrorCode.PROJECTS_LED_LIMIT
        )
        async with h.db() as session:
            assert await session.scalar(select(func.count()).select_from(models.Project)) == 5
            await session.execute(
                update(models.Project)
                .where(models.Project.id == projects[0].id)
                .values(status="abandoned")
            )
        response = await c.post("/api/v1/projects", json=BODY)
    assert response.status_code == 201, response.text


async def test_get_project_visibility(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    m = await h.user()
    event = await make_hackathon(h)
    public = await seed_project(h, a)
    hidden = await seed_project(h, a, members=[m], visibility="members", name="Secret")
    team = await seed_project(h, a, event_id=event.id, team_status="forming", name="Team")
    async with await h.client(b) as c:
        response = await c.get(f"/api/v1/projects/{public.id}")
        assert response.status_code == 200, response.text
        assert response.json()["view"] == "full"
        response = await c.get(f"/api/v1/projects/{team.id}")
        assert response.status_code == 200, response.text
        assert response.json()["view"] == "full"
        response = await c.get(f"/api/v1/projects/{hidden.id}")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "view": "limited",
            "id": str(hidden.id),
            "name": "Secret",
            "area_keys": ["backend"],
            "allowed_actions": [],
        }
        assert_problem(await c.get(f"/api/v1/projects/{uuid4()}"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(m) as c:
        response = await c.get(f"/api/v1/projects/{hidden.id}")
    assert response.status_code == 200, response.text
    assert response.json()["view"] == "full"
    assert {row["user_id"] for row in response.json()["members"]} == {str(a.id), str(m.id)}
    async with h.anonymous() as c:
        assert_problem(await c.get(f"/api/v1/projects/{public.id}"), 401, ErrorCode.UNAUTHENTICATED)


async def test_list_projects_filters(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    m = await h.user()
    event = await make_hackathon(h)
    recruiting = await seed_project(
        h, a, recruiting={"areas": ["backend"], "note": "Join"}, name="Recruiting"
    )
    hidden = await seed_project(
        h, a, members=[m], visibility="members", status="active", area_keys=("design",)
    )
    team = await seed_project(
        h,
        a,
        event_id=event.id,
        team_status="forming",
        status="active",
        area_keys=("ml_ai",),
        name="Team",
    )

    async def ids(c: Any, query: str = "") -> list[str]:
        response = await c.get(f"/api/v1/projects{query}")
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["items"]]

    async with await h.client(b) as c:
        response = await c.get("/api/v1/projects")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == [str(team.id), str(hidden.id), str(recruiting.id)]
        assert [item["view"] for item in items] == ["full", "limited", "full"]
        assert await ids(c, "?status=idea") == [str(recruiting.id)]
        assert await ids(c, "?area=backend") == [str(recruiting.id)]
        assert await ids(c, "?area=unknown") == []
        assert await ids(c, "?recruiting=true") == [str(recruiting.id)]
        assert await ids(c, "?recruiting=false") == [str(team.id), str(hidden.id)]
        assert await ids(c, f"?event={event.id}") == [str(team.id)]
        assert await ids(c, f"?member={m.id}") == [str(hidden.id)]
        assert_problem(
            await c.get("/api/v1/projects?status=bogus"), 422, ErrorCode.VALIDATION_ERROR
        )


async def test_list_projects_paging(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    projects = [await seed_project(h, a, name=f"P{index}") for index in range(3)]
    seen: list[str] = []
    cursor: str | None = None
    async with await h.client(a) as c:
        for page in range(3):
            query = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
            response = await c.get(f"/api/v1/projects{query}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["id"])
            cursor = body["next_cursor"]
            if page < 2:
                assert cursor is not None
    assert cursor is None
    assert seen == [str(project.id) for project in reversed(projects)]


async def test_list_my_projects(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    event = await make_hackathon(h)
    own = await seed_project(h, a, name="Own")
    team = await seed_project(
        h, b, members=[a], event_id=event.id, team_status="forming", name="Team"
    )
    await seed_project(h, b, name="Other")
    async with await h.client(a) as c:
        response = await c.get("/api/v1/me/projects")
        assert response.status_code == 200, response.text
        items = response.json()
        assert {item["id"] for item in items} == {str(own.id), str(team.id)}
        for item in items:
            assert item["view"] == "full"
            assert {"status", "visibility", "lead_user_id", "recruiting", "area_keys"} <= set(item)
            assert "members" not in item
        response = await c.get("/api/v1/me/projects?status=forming")
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == [str(team.id)]
        response = await c.get("/api/v1/me/projects?status=active")
        assert response.status_code == 200, response.text
        assert response.json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get("/api/v1/me/projects"), 401, ErrorCode.UNAUTHENTICATED)


async def test_edit_project_fields(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    project = await seed_project(h, a)
    closed = await seed_project(h, a, status="abandoned")
    h.advance(timedelta(hours=1))
    async with await h.client(a) as c:
        response = await c.patch(
            f"/api/v1/projects/{project.id}",
            json={
                "name": "New name",
                "description_md": "New description",
                "goal": "New goal",
                "links": ["https://example.com/y"],
                "area_keys": ["ml_ai"],
                "visibility": "members",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "New name"
        assert body["description_md"] == "New description"
        assert body["goal"] == "New goal"
        assert body["links"] == ["https://example.com/y"]
        assert body["area_keys"] == ["ml_ai"]
        assert body["visibility"] == "members"
        assert body["last_lead_action_at"] == h.now.isoformat().replace("+00:00", "Z")
        assert_problem(
            await c.patch(f"/api/v1/projects/{uuid4()}", json={}), 404, ErrorCode.NOT_FOUND
        )
        assert_problem(
            await c.patch(f"/api/v1/projects/{closed.id}", json={}),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with await h.client(b) as c:
        assert_problem(
            await c.patch(f"/api/v1/projects/{project.id}", json={}), 403, ErrorCode.FORBIDDEN
        )
    async with h.anonymous() as c:
        assert_problem(
            await c.patch(f"/api/v1/projects/{project.id}", json={}),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
    async with h.db() as session:
        keys = list(
            await session.scalars(
                select(models.Area.key)
                .join(models.ProjectArea, models.ProjectArea.area_id == models.Area.id)
                .where(models.ProjectArea.project_id == project.id)
            )
        )
        assert keys == ["ml_ai"]
    jobs = [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]
    assert len(jobs) == 1


async def test_edit_project_noop(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    project = await seed_project(h, a)
    h.advance(timedelta(hours=1))
    async with await h.client(a) as c:
        response = await c.patch(f"/api/v1/projects/{project.id}", json={})
    assert response.status_code == 200, response.text
    async with h.db() as session:
        row = await session.get(models.Project, project.id)
        assert row is not None
        assert row.last_lead_action_at == project.last_lead_action_at
        assert row.updated_at == project.updated_at
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_project_recruiting(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    project = await seed_project(h, a)
    event = await make_hackathon(h)
    team = await seed_project(h, a, event_id=event.id, team_status="forming")
    recruiting = {"areas": ["backend"], "note": "Need a backend dev"}
    async with await h.client(a) as c:
        response = await c.patch(f"/api/v1/projects/{project.id}", json={"recruiting": recruiting})
        assert response.status_code == 200, response.text
        assert response.json()["recruiting"] == recruiting
        async with h.db() as session:
            [item] = list(await session.scalars(select(models.FeedItem)))
            assert item.kind == "project_recruiting"
            assert item.subject_type == "project"
            assert item.subject_id == project.id
            assert item.hidden_at is None
            assert item.actor_id == a.id
        jobs = [
            job
            for job in await h.pending_jobs("refresh_recommendations")
            if job["status"] == "todo"
        ]
        assert len(jobs) == 1
        for keys in (["unknown"], ["backend", "backend"]):
            assert_problem(
                await c.patch(
                    f"/api/v1/projects/{project.id}",
                    json={"recruiting": {"areas": keys, "note": "Join"}},
                ),
                422,
                ErrorCode.VALIDATION_ERROR,
            )
        h.advance(timedelta(hours=1))
        response = await c.patch(f"/api/v1/projects/{project.id}", json={"recruiting": None})
        assert response.status_code == 200, response.text
        assert response.json()["recruiting"] is None
        assert_problem(
            await c.patch(f"/api/v1/projects/{team.id}", json={"recruiting": recruiting}),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.db() as session:
        [item] = list(await session.scalars(select(models.FeedItem)))
        assert item.hidden_at == h.now


async def test_project_visibility_hides_feed(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    project = await seed_project(h, a)
    async with await h.client(a) as c:
        response = await c.patch(
            f"/api/v1/projects/{project.id}",
            json={"recruiting": {"areas": ["backend"], "note": "Join"}},
        )
        assert response.status_code == 200, response.text
        async with h.db() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(models.FeedItem)
                    .where(models.FeedItem.hidden_at.is_(None))
                )
                == 1
            )
        h.advance(timedelta(hours=1))
        response = await c.patch(f"/api/v1/projects/{project.id}", json={"visibility": "members"})
        assert response.status_code == 200, response.text
    async with h.db() as session:
        [item] = list(await session.scalars(select(models.FeedItem)))
        assert item.hidden_at == h.now
    async with await h.client(b) as c:
        response = await c.get(f"/api/v1/projects/{project.id}")
        assert response.status_code == 200, response.text
        assert response.json()["view"] == "limited"


async def test_project_lifecycle_walk(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    project = await seed_project(h, a, members=[b])
    h.advance(timedelta(hours=1))
    async with await h.client(a) as c:
        response = await c.get(f"/api/v1/projects/{project.id}")
        assert response.json()["allowed_actions"] == [
            "edit",
            "invite",
            "leave",
            "start",
            "pause",
            "abandon",
            "step_down",
            "transfer_lead",
        ]
        response = await c.post(f"/api/v1/projects/{project.id}/start")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"
        assert response.json()["last_lead_action_at"] == h.now.isoformat().replace("+00:00", "Z")
        assert "start" not in response.json()["allowed_actions"]
        assert_problem(
            await c.post(f"/api/v1/projects/{project.id}/start"), 409, ErrorCode.INVALID_STATE
        )
        response = await c.post(f"/api/v1/projects/{project.id}/pause")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "paused"
        assert "start" in response.json()["allowed_actions"]
        assert "pause" not in response.json()["allowed_actions"]
        async with h.db() as session:
            [notification] = list(await session.scalars(select(models.Notification)))
            assert notification.type == "project_paused"
            assert notification.user_id == b.id
            assert isinstance(notification.payload, dict)
            assert notification.payload["reason"]
            assert notification.payload["status"] == "paused"
        response = await c.post(f"/api/v1/projects/{project.id}/abandon")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "abandoned"
        assert not {"start", "pause", "abandon", "continue", "step_down", "transfer_lead"} & set(
            response.json()["allowed_actions"]
        )
        for transition in ("start", "pause", "abandon", "continue"):
            assert_problem(
                await c.post(f"/api/v1/projects/{project.id}/{transition}"),
                409,
                ErrorCode.INVALID_STATE,
            )
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        row = await session.get(models.Project, project.id)
        assert row is not None
        assert row.last_progress_at == project.last_progress_at
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert not [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]


async def test_continue_project(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    project = await seed_project(h, a, status="shipped")
    event = await make_hackathon(h)
    team = await seed_project(h, a, status="shipped", event_id=event.id, team_status="finished")
    idea = await seed_project(h, a)
    async with await h.client(a) as c:
        for row in (project, team):
            response = await c.get(f"/api/v1/projects/{row.id}")
            assert response.status_code == 200, response.text
            assert "continue" in response.json()["allowed_actions"]
            assert "pause" not in response.json()["allowed_actions"]
            assert {"transfer_lead", "step_down"} <= set(response.json()["allowed_actions"])
            response = await c.post(f"/api/v1/projects/{row.id}/continue")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "active"
            assert response.json()["team_status"] == row.team_status
            assert_problem(
                await c.post(f"/api/v1/projects/{row.id}/continue"), 409, ErrorCode.INVALID_STATE
            )
        assert_problem(
            await c.post(f"/api/v1/projects/{idea.id}/continue"), 409, ErrorCode.INVALID_STATE
        )


@pytest.mark.parametrize("team_status", ["forming", "locked"])
async def test_project_team_lifecycle_guard(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    event = await make_hackathon(h)
    team = await seed_project(h, a, event_id=event.id, team_status=team_status)
    async with await h.client(a) as c:
        for transition in ("start", "pause", "abandon", "continue"):
            assert_problem(
                await c.post(f"/api/v1/projects/{team.id}/{transition}"),
                409,
                ErrorCode.INVALID_STATE,
            )
        response = await c.get(f"/api/v1/projects/{team.id}")
        assert response.status_code == 200, response.text
        assert "edit" in response.json()["allowed_actions"]


async def test_project_lifecycle_feed(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    project = await seed_project(h, a)
    async with await h.client(a) as c:
        response = await c.patch(
            f"/api/v1/projects/{project.id}",
            json={"recruiting": {"areas": ["backend"], "note": "Join"}},
        )
        assert response.status_code == 200, response.text
        async with h.db() as session:
            [original] = list(await session.scalars(select(models.FeedItem)))
            assert original.hidden_at is None
        async with await h.client(b) as other:
            response = await other.get(f"/api/v1/projects/{project.id}")
            assert "request_join" in response.json()["allowed_actions"]
        for transition, expected_count in (("pause", 0), ("start", 1), ("abandon", 0)):
            h.advance(timedelta(hours=1))
            response = await c.post(f"/api/v1/projects/{project.id}/{transition}")
            assert response.status_code == 200, response.text
            async with h.db() as session:
                items = list(await session.scalars(select(models.FeedItem)))
                visible = [item for item in items if item.hidden_at is None]
                assert len(visible) == expected_count
                if transition == "start":
                    assert len(items) == 2
                    assert visible[0].id != original.id


async def test_project_lifecycle_lead_only(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a = await h.user()
    b = await h.user()
    admin = await h.admin()
    project = await seed_project(h, a, members=[b])
    async with await h.client(b) as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{project.id}/start"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(admin) as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{project.id}/pause"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(a) as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{UUID(int=0)}/start"), 404, ErrorCode.NOT_FOUND
        )
