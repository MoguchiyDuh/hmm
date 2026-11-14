import itertools
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import uuid4

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
