import asyncio
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


async def set_formation(h: Harness, event: models.Event, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(**values)
        )


async def member_row(h: Harness, project_id: UUID, user_id: UUID) -> models.ProjectMember:
    async with h.db() as session:
        return (
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project_id,
                    models.ProjectMember.user_id == user_id,
                )
            )
        ).one()


async def refresh_jobs(h: Harness) -> list[dict[str, Any]]:
    return [
        job for job in await h.pending_jobs("refresh_recommendations") if job["status"] == "todo"
    ]


async def leaderless(h: Harness, project: models.Project) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(lead_user_id=None, lead_succession_excluded=[project.lead_user_id])
        )
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == project.id)
            .values(role="member")
        )


async def test_remove_project_member(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c])
    member = await member_row(h, project.id, b.id)
    async with h.db() as session:
        confirmation = models.Confirmation(
            fact_type="project_member",
            fact_id=member.id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source="lead",
            ignored=False,
        )
        session.add(confirmation)
        await session.flush()
    h.advance(timedelta(hours=1))
    async with await h.client(a) as client:
        response = await client.request(
            "DELETE",
            f"/api/v1/projects/{project.id}/members/{b.id}",
            json={"reason": "stopped showing up"},
        )
    assert response.status_code == 204, response.text
    assert response.content == b""
    member = await member_row(h, project.id, b.id)
    assert member.left_at == h.now
    assert member.left_reason == "removed"
    async with h.db() as session:
        [audit] = list(await session.scalars(select(models.AuditLog)))
        assert audit.action == "member_remove"
        assert audit.actor_id == a.id
        assert audit.actor_kind == "user"
        assert audit.entity_type == "project_member"
        assert audit.entity_id == str(member.id)
        assert audit.reason == "stopped showing up"
        [notice] = list(await session.scalars(select(models.Notification)))
        assert notice.user_id == b.id
        assert notice.type == "project_member_removed"
        assert isinstance(notice.payload, dict)
        assert notice.payload["reason"] == "stopped showing up"
        assert notice.payload["member"] == {"user_id": str(b.id), "display_name": b.display_name}
        row = await session.get_one(models.Project, project.id)
        assert row.last_lead_action_at == h.now
        assert await session.get(models.Confirmation, confirmation.id) is not None
    assert len(await refresh_jobs(h)) == 1


async def test_remove_project_member_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c])
    closed = await seed_project(h, a, members=[b], status="abandoned")
    async with await h.client(c) as client:
        assert_problem(
            await client.delete(f"/api/v1/projects/{project.id}/members/{b.id}"),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with await h.client(a) as client:
        for project_id, user_id, status, code in (
            (project.id, a.id, 409, ErrorCode.INVALID_STATE),
            (project.id, uuid4(), 404, ErrorCode.NOT_FOUND),
            (closed.id, b.id, 409, ErrorCode.INVALID_STATE),
            (uuid4(), b.id, 404, ErrorCode.NOT_FOUND),
        ):
            assert_problem(
                await client.delete(f"/api/v1/projects/{project_id}/members/{user_id}"),
                status,
                code,
            )
        response = await client.delete(f"/api/v1/projects/{project.id}/members/{b.id}")
        assert response.status_code == 204, response.text
        assert_problem(
            await client.delete(f"/api/v1/projects/{project.id}/members/{b.id}"),
            404,
            ErrorCode.NOT_FOUND,
        )


async def test_leave_project(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b, c])
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    member = await member_row(h, project.id, b.id)
    assert member.left_at == h.now
    assert member.left_reason == "left"
    async with h.db() as session:
        notices = list(await session.scalars(select(models.Notification)))
        assert {notice.user_id for notice in notices} == {a.id, c.id}
        assert len(notices) == 2
        for notice in notices:
            assert notice.type == "project_member_left"
            assert isinstance(notice.payload, dict)
            assert notice.payload["member"] == {
                "user_id": str(b.id),
                "display_name": b.display_name,
            }
            assert notice.payload["member_count"] == 2
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id == a.id
    assert len(await refresh_jobs(h)) == 1


async def test_leave_project_as_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b])
    async with h.db() as session:
        session.add(
            models.LeadOffer(
                project_id=project.id,
                user_id=b.id,
                source="transfer",
                offered_by=a.id,
                reason="Take over",
                status="pending",
                expires_at=h.now + timedelta(days=7),
            )
        )
    async with await h.client(a) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    assert (await member_row(h, project.id, a.id)).left_reason == "left"
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id is None
        [audit] = list(await session.scalars(select(models.AuditLog)))
        assert audit.action == "lead_step_down"
        assert audit.actor_id == a.id
        assert audit.actor_kind == "user"
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {b.id}
        assert {notice.type for notice in notices} == {"project_member_left", "project_lead_offer"}
        offers = list(await session.scalars(select(models.LeadOffer)))
        [transfer] = [offer for offer in offers if offer.source == "transfer"]
        assert transfer.status == "cancelled"
        [succession] = [offer for offer in offers if offer.source == "succession"]
        assert succession.status == "pending"
        assert succession.user_id == b.id
        assert succession.expires_at == h.now + timedelta(days=3)
        assert succession.offered_by is None
    assert len(await refresh_jobs(h)) == 1


async def test_leave_project_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, a, members=[b])
    async with await h.client(c) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{project.id}/leave"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(b) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{uuid4()}/leave"), 404, ErrorCode.NOT_FOUND
        )
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
        assert response.status_code == 204, response.text
        assert_problem(
            await client.post(f"/api/v1/projects/{project.id}/leave"), 403, ErrorCode.FORBIDDEN
        )


async def test_claim_project_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b], status="paused")
    await leaderless(h, project)
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/claim-lead")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead_user_id"] == str(b.id)
    assert body["status"] == "paused"
    assert "start" in body["allowed_actions"]
    assert "claim_lead" not in body["allowed_actions"]
    assert (await member_row(h, project.id, b.id)).role == "lead"
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_succession_excluded == []
        [audit] = list(await session.scalars(select(models.AuditLog)))
        assert audit.action == "lead_assign"
        assert audit.actor_id == b.id
        assert audit.actor_kind == "user"
        assert isinstance(audit.after, dict)
        assert audit.after["source"] == "claim"
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
    assert len(await refresh_jobs(h)) == 1


@pytest.mark.parametrize("team_status", ["finished", "withdrawn"])
async def test_claim_continued_team_lead(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    event = await make_hackathon(h)
    project = await seed_project(
        h, a, members=[b], status="paused", event_id=event.id, team_status=team_status
    )
    await leaderless(h, project)
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/claim-lead")
    assert response.status_code == 200, response.text
    assert response.json()["lead_user_id"] == str(b.id)
    assert response.json()["status"] == "paused"
    assert response.json()["team_status"] == team_status
    assert (await member_row(h, project.id, b.id)).role == "lead"


async def test_claim_project_lead_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    paused = await seed_project(h, a, members=[b], status="paused")
    active = await seed_project(h, a, members=[b], status="active")
    led = await seed_project(h, a, members=[b], status="paused")
    await leaderless(h, paused)
    await leaderless(h, active)
    event = await make_hackathon(h)
    team = await seed_project(h, a, members=[b], event_id=event.id, team_status="forming")
    async with await h.client(c) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{paused.id}/claim-lead"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(b) as client:
        for row in (led, active, team):
            assert_problem(
                await client.post(f"/api/v1/projects/{row.id}/claim-lead"),
                409,
                ErrorCode.INVALID_STATE,
            )
        response = await client.post(f"/api/v1/projects/{paused.id}/claim-lead")
        assert response.status_code == 200, response.text
    async with await h.client(a) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{paused.id}/claim-lead"),
            409,
            ErrorCode.INVALID_STATE,
        )


async def test_claim_project_lead_concurrently(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b], status="paused")
    await leaderless(h, project)
    async with await h.client(a) as first, await h.client(b) as second:
        responses = await asyncio.gather(
            first.post(f"/api/v1/projects/{project.id}/claim-lead"),
            second.post(f"/api/v1/projects/{project.id}/claim-lead"),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(
        response.json()["lead_user_id"] for response in responses if response.status_code == 200
    )
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert str(row.lead_user_id) == winner
        leads = list(
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.role == "lead",
                    models.ProjectMember.left_at.is_(None),
                )
            )
        )
        assert len(leads) == 1
        assert str(leads[0].user_id) == winner


async def test_claim_project_lead_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b = await h.user(), await h.user()
    project = await seed_project(h, a, members=[b], status="paused")
    await leaderless(h, project)
    for index in range(5):
        await seed_project(h, b, name=f"Led {index}")
    async with await h.client(b) as client:
        assert_problem(
            await client.post(f"/api/v1/projects/{project.id}/claim-lead"),
            409,
            ErrorCode.PROJECTS_LED_LIMIT,
        )
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.lead_user_id is None


async def test_project_invite_cap(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    admin = await h.admin()
    project = await seed_project(h, a, members=[b])
    async with h.db() as session:
        session.add(models.Setting(key="project_size_max", value=2, updated_by=admin.id))
    async with await h.client(a) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{project.id}/invites", json={"user_id": str(c.id)}
            ),
            409,
            ErrorCode.PROJECT_FULL,
        )
        async with h.db() as session:
            assert await session.scalar(select(func.count()).select_from(models.Invite)) == 0
            await session.execute(
                update(models.Setting)
                .where(models.Setting.key == "project_size_max")
                .values(value=3)
            )
        response = await client.post(
            f"/api/v1/projects/{project.id}/invites", json={"user_id": str(c.id)}
        )
        assert response.status_code == 201, response.text
    assert len(await refresh_jobs(h)) == 1


async def seed_request(
    h: Harness, project: models.Project, user: models.User
) -> models.JoinRequest:
    async with h.db() as session:
        request = models.JoinRequest(
            project_id=project.id,
            event_id=project.event_id,
            user_id=user.id,
            note="Join",
            status="pending",
        )
        session.add(request)
        await session.flush()
        await session.refresh(request)
        return request


async def test_project_joins_keep_capacity(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, member, b, c = await h.user(), await h.user(), await h.user(), await h.user()
    admin = await h.admin()
    project = await seed_project(h, a, members=[member])
    async with h.db() as session:
        session.add(models.Setting(key="project_size_max", value=3, updated_by=admin.id))
    requests = [await seed_request(h, project, user) for user in (b, c)]
    async with await h.client(a) as first, await h.client(a) as second:
        responses = await asyncio.gather(
            first.post(f"/api/v1/join-requests/{requests[0].id}/accept"),
            second.post(f"/api/v1/join-requests/{requests[1].id}/accept"),
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    failure = next(response for response in responses if response.status_code == 409)
    assert_problem(failure, 409, ErrorCode.PROJECT_FULL)
    winner = next(
        response.json()["user_id"] for response in responses if response.status_code == 200
    )
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
            == 3
        )
        [notice] = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == "join_request_accepted"
                )
            )
        )
        assert str(notice.user_id) == winner
    assert len(await refresh_jobs(h)) == 1


@pytest.mark.parametrize("via", ["invite", "join_request"])
async def test_project_joined_notices(harness: Harness, via: str) -> None:
    h = harness
    await h.seed_areas()
    a, b, joiner = await h.user(), await h.user(), await h.user()
    project = await seed_project(
        h, a, members=[b], recruiting={"areas": ["backend"], "note": "Join"}
    )
    async with await h.client(joiner) as member_client, await h.client(a) as lead_client:
        if via == "invite":
            response = await lead_client.post(
                f"/api/v1/projects/{project.id}/invites", json={"user_id": str(joiner.id)}
            )
            assert response.status_code == 201, response.text
            response = await member_client.post(f"/api/v1/invites/{response.json()['id']}/accept")
        else:
            response = await member_client.post(
                f"/api/v1/projects/{project.id}/join-requests", json={"note": "Join"}
            )
            assert response.status_code == 201, response.text
            response = await lead_client.post(
                f"/api/v1/join-requests/{response.json()['id']}/accept"
            )
        assert response.status_code == 200, response.text
    async with h.db() as session:
        notices = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == "project_member_joined"
                )
            )
        )
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {a.id, b.id}
        for notice in notices:
            assert isinstance(notice.payload, dict)
            assert notice.payload["member"] == {
                "user_id": str(joiner.id),
                "display_name": joiner.display_name,
            }
            assert notice.payload["member_count"] == 3
    assert len(await refresh_jobs(h)) == 1


async def test_team_core_join_has_no_project_joined_notice(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, joiner = await h.user(), await h.user()
    event = await make_hackathon(h)
    await set_formation(h, event, phase="open", formation_at=h.now + timedelta(days=5))
    project = await seed_project(h, a, event_id=event.id, team_status="forming")
    async with h.db() as session:
        session.add(
            models.Enrollment(
                event_id=event.id,
                user_id=joiner.id,
                status="active",
                source="self",
                willing_to_lead=False,
                status_at=h.now,
            )
        )
    request = await seed_request(h, project, joiner)
    async with await h.client(a) as client:
        response = await client.post(f"/api/v1/join-requests/{request.id}/accept")
    assert response.status_code == 200, response.text
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Notification)
                .where(models.Notification.type == "project_member_joined")
            )
            == 0
        )


async def test_active_team_membership_guards(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    a, b, c = await h.user(), await h.user(), await h.user()
    event = await make_hackathon(h)
    await set_formation(h, event, phase="locked")
    project = await seed_project(h, a, members=[b, c], event_id=event.id, team_status="active")
    async with h.db() as session:
        await session.execute(
            update(models.ProjectMember)
            .where(models.ProjectMember.project_id == project.id)
            .values(is_team_row=True)
        )
    async with await h.client(a) as client:
        assert_problem(
            await client.delete(f"/api/v1/projects/{project.id}/members/{b.id}"),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with await h.client(b) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    assert (await member_row(h, project.id, b.id)).left_reason == "left"
    await set_formation(h, event, phase="review", review_ends_at=h.now + timedelta(days=1))
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(team_status="review", review_ends_at=h.now + timedelta(days=1))
        )
    async with await h.client(a) as client:
        response = await client.delete(f"/api/v1/projects/{project.id}/members/{c.id}")
    assert response.status_code == 204, response.text
    assert (await member_row(h, project.id, c.id)).left_reason == "removed"
