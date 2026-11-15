from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from harness import Harness, assert_problem
from sqlalchemy import func, select

from hmm import models
from hmm.common import ErrorCode

RELEASE_BODY: dict[str, Any] = {
    "title": "v1",
    "notes_md": "n",
    "repo_url": "https://github.com/a/b",
}


async def seed_area(h: Harness, key: str = "backend") -> models.Area:
    async with h.db() as session:
        area = models.Area(key=key, name=key.title(), description=key, sort=10)
        session.add(area)
        await session.flush()
        await session.refresh(area)
        return area


async def seed_project(
    h: Harness,
    lead: models.User,
    *,
    members: Sequence[models.User] = (),
    name: str = "Project",
    visibility: str = "public",
    status: str = "idea",
    event_id: Any = None,
    area_keys: Sequence[str] = (),
    recruiting: dict[str, Any] | None = None,
    team_status: str | None = None,
    joined_at: datetime | None = None,
    left_at: datetime | None = None,
) -> tuple[models.Project, list[models.ProjectMember]]:
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
            origin="standalone" if team_status is None else "formation",
            last_progress_at=h.now,
        )
        session.add(project)
        await session.flush()
        for key in area_keys:
            area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
            assert area_id is not None
            session.add(models.ProjectArea(project_id=project.id, area_id=area_id))
        member_rows: list[models.ProjectMember] = []
        for user in (lead, *members):
            is_lead = user.id == lead.id
            row = models.ProjectMember(
                project_id=project.id,
                event_id=event_id if team_status is not None else None,
                user_id=user.id,
                role="lead" if is_lead else "member",
                state="confirmed",
                trust="self",
                joined_via="create",
                is_team_row=False,
                joined_at=h.now if joined_at is None else joined_at,
                confirmed_at=h.now if joined_at is None else joined_at,
                left_at=left_at,
            )
            session.add(row)
            member_rows.append(row)
        await session.flush()
        for row in member_rows:
            await session.refresh(row)
        await session.refresh(project)
        return project, member_rows


def contributions(
    *users: models.User, contributed: bool = True, area: str | None = None
) -> list[dict[str, Any]]:
    return [
        {"user_id": str(user.id), "contributed": contributed, "area_key": area} for user in users
    ]


async def rows(h: Harness, model: type[Any], *where: Any) -> list[Any]:
    async with h.db() as session:
        return list(await session.scalars(select(model).where(*where)))


async def count(h: Harness, model: type[Any], *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def notices(h: Harness, user: models.User, type: str) -> list[models.Notification]:
    return await rows(
        h,
        models.Notification,
        models.Notification.user_id == user.id,
        models.Notification.type == type,
    )


def payload(row: models.Notification | models.FeedItem) -> dict[str, Any]:
    return cast(dict[str, Any], row.payload)


async def test_create_release(harness: Harness) -> None:
    h = harness
    area = await seed_area(h)
    lead, member, other = await h.user(), await h.user(), await h.user()
    project, member_rows = await seed_project(
        h,
        lead,
        members=[member, other],
        status="active",
        area_keys=["backend"],
        joined_at=h.now - timedelta(days=10),
    )
    lead_row = next(row for row in member_rows if row.user_id == lead.id)
    member_row = next(row for row in member_rows if row.user_id == member.id)
    other_row = next(row for row in member_rows if row.user_id == other.id)
    h.advance(timedelta(hours=1))
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/releases",
            json={
                **RELEASE_BODY,
                "contributions": [
                    {"user_id": str(lead.id), "contributed": True},
                    {"user_id": str(member.id), "contributed": True, "area_key": "backend"},
                    {"user_id": str(other.id), "contributed": False},
                ],
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created_by"] == str(lead.id)
    assert body["title"] == "v1"
    assert body["notes_md"] == "n"
    assert body["repo_url"] == "https://github.com/a/b"
    assert body["contributions"] == [
        {"user_id": str(lead.id), "contributed": True, "area_key": None},
        {"user_id": str(member.id), "contributed": True, "area_key": "backend"},
        {"user_id": str(other.id), "contributed": False, "area_key": None},
    ]
    async with h.db() as session:
        [release] = list(await session.scalars(select(models.ProjectRelease)))
        assert datetime.fromisoformat(body["created_at"]) == release.created_at
        assert (
            await session.scalar(select(func.count()).select_from(models.ReleaseContribution)) == 3
        )
        project_row = await session.get(models.Project, project.id)
        assert project_row is not None
        assert project_row.status == "shipped"
        assert project_row.paused_reason is None
        assert project_row.last_progress_at == h.now
        confirmations = list(
            await session.scalars(
                select(models.Confirmation).where(
                    models.Confirmation.withdrawn_at.is_(None),
                    models.Confirmation.fact_type == "project_member",
                )
            )
        )
        assert len(confirmations) == 1
        assert confirmations[0].fact_id == member_row.id
        assert confirmations[0].subject_user_id == member.id
        assert confirmations[0].confirmer_id == lead.id
        assert confirmations[0].source == "lead"
        assert confirmations[0].ignored is False
        member_after = await session.get(models.ProjectMember, member_row.id)
        assert member_after is not None and member_after.trust == "trusted"
        [item] = list(
            await session.scalars(
                select(models.FeedItem).where(models.FeedItem.kind == "project_shipped")
            )
        )
        assert item.subject_type == "project" and item.subject_id == project.id
        assert item.actor_id == lead.id
        assert payload(item)["release_id"] == str(release.id)
        assert item.area_ids == [area.id]
        windows = list(await session.scalars(select(models.ReviewWindow)))
        assert len(windows) == 1
        assert windows[0].subject_type == "project"
        assert windows[0].subject_id == project.id
        assert windows[0].opens_at == h.now
        assert windows[0].closes_at == h.now + timedelta(days=7)
        assert windows[0].participants == {
            str(lead.id): "lead",
            str(member.id): "member",
            str(other.id): "member",
        }
        assert other_row.id is not None
    for user in (member, other):
        [released] = await notices(h, user, "project_released")
        assert payload(released)["release_title"] == "v1"
        assert payload(released)["notes_md"] == "n"
        assert payload(released)["actor"]["user_id"] == str(lead.id)
        [digest] = await notices(h, user, "contributions_confirm_digest")
        assert payload(digest)["release_title"] == "v1"
        assert payload(digest)["project_member_id"] == str(lead_row.id)
        assert len(await notices(h, user, "reviews_open")) == 1
    assert await notices(h, lead, "project_released") == []
    assert await notices(h, lead, "contributions_confirm_digest") == []
    assert len(await notices(h, lead, "reviews_open")) == 1
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert len(jobs) == 3
    assert {job["args"]["user_id"] for job in jobs} == {
        str(lead.id),
        str(member.id),
        str(other.id),
    }


async def test_release_contribution_coverage(harness: Harness) -> None:
    h = harness
    lead, member, stranger = await h.user(), await h.user(), await h.user()
    project, _ = await seed_project(h, lead, members=[member], status="active")
    path = f"/api/v1/projects/{project.id}/releases"
    h.advance(timedelta(hours=1))

    async def post(client: Any, body: dict[str, Any]) -> Any:
        return await client.post(path, json={**RELEASE_BODY, **body})

    async with await h.client(lead) as client:
        assert_problem(
            await post(client, {"contributions": contributions(lead)}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(
            await post(client, {"contributions": contributions(lead, member, stranger)}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(
            await post(client, {"contributions": contributions(lead, member, member)}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(await post(client, {"contributions": []}), 422, ErrorCode.VALIDATION_ERROR)
        assert_problem(
            await post(
                client,
                {
                    "contributions": [
                        {"user_id": str(lead.id), "contributed": True},
                        {"user_id": str(member.id), "contributed": True, "area_key": "unknown"},
                    ]
                },
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        response = await client.post(
            path,
            json={
                "title": "v1",
                "notes_md": "n",
                "contributions": contributions(lead, member),
            },
        )
        assert response.status_code == 422, response.text


async def test_release_member_tenure(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    old_left, recent_left, proposed = await h.user(), await h.user(), await h.user()
    project, member_rows = await seed_project(
        h, lead, members=[member, old_left, recent_left, proposed], status="active"
    )
    by_user = {row.user_id: row for row in member_rows}
    async with h.db() as session:
        old = await session.get(models.ProjectMember, by_user[old_left.id].id)
        assert old is not None
        old.left_at = h.now - timedelta(days=40)
        recent = await session.get(models.ProjectMember, by_user[recent_left.id].id)
        assert recent is not None
        recent.left_at = h.now - timedelta(days=10)
        proposal = await session.get(models.ProjectMember, by_user[proposed.id].id)
        assert proposal is not None
        proposal.state = "proposed"
        proposal.accept_by = h.now + timedelta(days=7)
    path = f"/api/v1/projects/{project.id}/releases"
    h.advance(timedelta(hours=1))
    async with await h.client(lead) as client:
        response = await client.post(
            path,
            json={
                **RELEASE_BODY,
                "contributions": contributions(lead, member, recent_left),
            },
        )
        assert response.status_code == 201, response.text
        assert_problem(
            await client.post(
                path,
                json={**RELEASE_BODY, "contributions": contributions(lead, member)},
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    assert (
        await count(
            h,
            models.ReleaseContribution,
            models.ReleaseContribution.user_id == recent_left.id,
        )
        == 1
    )
    assert (
        await count(
            h,
            models.ReleaseContribution,
            models.ReleaseContribution.user_id == old_left.id,
        )
        == 0
    )
    assert (
        await count(
            h,
            models.ReleaseContribution,
            models.ReleaseContribution.user_id == proposed.id,
        )
        == 0
    )


async def test_release_gates(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project, _ = await seed_project(h, lead, members=[member], status="active")
    path = f"/api/v1/projects/{project.id}/releases"
    body = {**RELEASE_BODY, "contributions": contributions(lead, member)}
    h.advance(timedelta(hours=1))
    async with await h.client(member) as client:
        assert_problem(await client.post(path, json=body), 403, ErrorCode.FORBIDDEN)
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{uuid4()}/releases", json=body),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.get(f"/api/v1/projects/{uuid4()}/releases"),
            404,
            ErrorCode.NOT_FOUND,
        )
    abandoned, _ = await seed_project(h, lead, members=[member], status="abandoned")
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{abandoned.id}/releases", json=body),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.anonymous() as client:
        assert_problem(await client.post(path, json=body), 401, ErrorCode.UNAUTHENTICATED)
    for cause in ("manual", "idle"):
        paused, _ = await seed_project(h, lead, members=[member], status="paused")
        async with h.db() as session:
            row = await session.get(models.Project, paused.id)
            assert row is not None
            row.paused_reason = cause
        async with await h.client(lead) as client:
            response = await client.post(f"/api/v1/projects/{paused.id}/releases", json=body)
        assert response.status_code == 201, response.text
        async with h.db() as session:
            row = await session.get(models.Project, paused.id)
            assert row is not None
            assert row.status == "shipped" and row.paused_reason is None


async def test_list_releases(harness: Harness) -> None:
    h = harness
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    project, member_rows = await seed_project(
        h, lead, members=[member], status="shipped", joined_at=h.now - timedelta(days=10)
    )
    member_row = member_rows[1]
    async with h.db() as session:
        for hours in (2, 1):
            release = models.ProjectRelease(
                project_id=project.id,
                title=f"r{hours}",
                notes_md="n",
                repo_url="https://github.com/a/b",
                demo_url=None,
                writeup_url=None,
                created_by=lead.id,
                created_at=h.now - timedelta(hours=hours),
            )
            session.add(release)
            await session.flush()
            session.add(
                models.ReleaseContribution(
                    release_id=release.id,
                    user_id=member.id,
                    contributed=True,
                    area_id=None,
                )
            )
        result_release = models.ProjectRelease(
            project_id=project.id,
            title="Hackathon result",
            notes_md="n",
            repo_url=None,
            demo_url=None,
            writeup_url=None,
            created_by=lead.id,
            created_at=h.now,
        )
        session.add(result_release)
        await session.flush()
    path = f"/api/v1/projects/{project.id}/releases"
    async with await h.client(member) as client:
        response = await client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["title"] for row in body] == ["Hackathon result", "r1", "r2"]
    assert body[0]["contributions"] == []
    assert body[1]["contributions"] == [
        {"user_id": str(member.id), "contributed": True, "area_key": None}
    ]
    assert body[2]["contributions"] == [
        {"user_id": str(member.id), "contributed": True, "area_key": None}
    ]
    assert member_row.id is not None
    private, _ = await seed_project(
        h, lead, members=[member], visibility="members", status="shipped"
    )
    private_path = f"/api/v1/projects/{private.id}/releases"
    async with await h.client(outsider) as client:
        assert_problem(await client.get(private_path), 404, ErrorCode.NOT_FOUND)
    async with await h.client(member) as client:
        response = await client.get(private_path)
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_release_repeat_window(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project, _ = await seed_project(
        h, lead, members=[member], status="active", joined_at=h.now - timedelta(days=10)
    )
    body = {**RELEASE_BODY, "contributions": contributions(lead, member)}
    start = h.now
    path = f"/api/v1/projects/{project.id}/releases"
    async with await h.client(lead) as client:
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        h.advance(timedelta(days=29))
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        h.advance(timedelta(days=1))
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
    assert await count(h, models.ProjectRelease) == 3
    windows = sorted(await rows(h, models.ReviewWindow), key=lambda row: row.opens_at)
    assert len(windows) == 2
    assert windows[0].opens_at == start
    assert windows[1].opens_at == h.now
    assert len(await notices(h, member, "reviews_open")) == 2
    assert len(await notices(h, member, "project_released")) == 3


async def test_release_retries_duplicate_nothing(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    project, member_rows = await seed_project(
        h, lead, members=[member], status="active", joined_at=h.now - timedelta(days=10)
    )
    member_row = member_rows[1]
    body = {**RELEASE_BODY, "contributions": contributions(lead, member)}
    path = f"/api/v1/projects/{project.id}/releases"
    h.advance(timedelta(hours=1))
    async with await h.client(lead) as client:
        for _ in range(2):
            response = await client.post(path, json=body)
            assert response.status_code == 201, response.text
    assert await count(h, models.ProjectRelease) == 2
    assert await count(h, models.ReleaseContribution) == 4
    confirmations = await rows(
        h,
        models.Confirmation,
        models.Confirmation.fact_id == member_row.id,
        models.Confirmation.withdrawn_at.is_(None),
    )
    assert len(confirmations) == 1 and confirmations[0].source == "lead"
    assert await count(h, models.ReviewWindow) == 1
    assert await count(h, models.FeedItem, models.FeedItem.kind == "project_shipped") == 2
    assert len(await notices(h, member, "contributions_confirm_digest")) == 2
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert {job["args"]["user_id"] for job in jobs} == {str(lead.id), str(member.id)}


async def test_release_end_to_end(harness: Harness) -> None:
    h = harness
    await seed_area(h)
    lead, member, other = await h.user(), await h.user(), await h.user()
    async with await h.client(lead) as client:
        response = await client.post(
            "/api/v1/projects",
            json={
                "name": "Chess engine",
                "description_md": "d",
                "goal": "g",
                "area_keys": ["backend"],
                "links": [],
                "visibility": "public",
            },
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
        assert response.json()["status"] == "idea"
        response = await client.patch(
            f"/api/v1/projects/{project_id}",
            json={"recruiting": {"areas": ["backend"], "note": "Need a backend dev"}},
        )
        assert response.status_code == 200, response.text
    async with h.db() as session:
        [recruiting] = list(
            await session.scalars(
                select(models.FeedItem).where(models.FeedItem.hidden_at.is_(None))
            )
        )
        assert recruiting.kind == "project_recruiting"
    request_ids = []
    for user in (member, other):
        async with await h.client(user) as client:
            response = await client.post(
                f"/api/v1/projects/{project_id}/join-requests", json={"note": "me"}
            )
            assert response.status_code == 201, response.text
            request_ids.append(response.json()["id"])
    async with await h.client(lead) as client:
        for request_id in request_ids:
            response = await client.post(f"/api/v1/join-requests/{request_id}/accept")
            assert response.status_code == 200, response.text
    async with await h.client(member) as client:
        milestone_ids = []
        for index in (1, 2):
            response = await client.post(
                f"/api/v1/projects/{project_id}/milestones",
                json={"title": f"m{index}", "position": index},
            )
            assert response.status_code == 201, response.text
            milestone_ids.append(response.json()["id"])
        response = await client.patch(
            f"/api/v1/projects/{project_id}/milestones/{milestone_ids[0]}",
            json={"done": True},
        )
        assert response.status_code == 200, response.text
    async with await h.client(other) as client:
        response = await client.post(
            f"/api/v1/projects/{project_id}/updates",
            json={"body_md": "progress", "links": []},
        )
        assert response.status_code == 201, response.text
    async with h.db() as session:
        row = await session.get(models.Project, project_id)
        assert row is not None and row.status == "active"
    lead_id = lead.id
    member_id = member.id
    other_id = other.id
    h.advance(timedelta(days=10))
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{project_id}/releases",
            json={
                **RELEASE_BODY,
                "contributions": contributions(lead, member, other),
            },
        )
    assert response.status_code == 201, response.text
    async with h.db() as session:
        row = await session.get(models.Project, project_id)
        assert row is not None and row.status == "shipped"
        confirmations = list(
            await session.scalars(
                select(models.Confirmation).where(
                    models.Confirmation.fact_type == "project_member",
                    models.Confirmation.source == "lead",
                    models.Confirmation.withdrawn_at.is_(None),
                )
            )
        )
        assert len(confirmations) == 2
        assert {confirmation.subject_user_id for confirmation in confirmations} == {
            member_id,
            other_id,
        }
        assert lead_id not in {confirmation.subject_user_id for confirmation in confirmations}
        windows = list(await session.scalars(select(models.ReviewWindow)))
        assert len(windows) == 1
        assert windows[0].participants == {
            str(lead_id): "lead",
            str(member_id): "member",
            str(other_id): "member",
        }
        [recruiting] = list(
            await session.scalars(
                select(models.FeedItem).where(models.FeedItem.kind == "project_recruiting")
            )
        )
        assert recruiting.hidden_at == h.now
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.FeedItem)
                .where(models.FeedItem.kind == "project_shipped")
            )
            == 1
        )
    assert len(await notices(h, member, "project_released")) == 1
    assert len(await notices(h, other, "project_released")) == 1
    assert len(await notices(h, member, "contributions_confirm_digest")) == 1
    assert len(await notices(h, other, "contributions_confirm_digest")) == 1
    jobs = [job for job in await h.pending_jobs("recompute_member") if job["status"] == "todo"]
    assert {job["args"]["user_id"] for job in jobs} == {
        str(lead_id),
        str(member_id),
        str(other_id),
    }
