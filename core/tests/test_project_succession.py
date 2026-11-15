from datetime import timedelta
from typing import Any

import pytest
from harness import Harness
from hmm_contracts.common import AreaImportance, ErrorCode, EventKind, EventOrigin, EventStatus
from hmm_core import models
from hmm_core.catalog import service as catalog
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem


async def make_event(h: Harness) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title="Hack",
                starts_at=h.now + timedelta(days=30),
                ends_at=h.now + timedelta(days=32),
                timezone="UTC",
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
    members: tuple[models.User, ...] = (),
    status: str = "idea",
    team_status: str | None = None,
    event_id: Any = None,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event_id,
            name="Project",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=lead.id,
            status=status,
            team_status=team_status,
            origin="standalone" if team_status is None else "formation",
            last_progress_at=h.now,
        )
        session.add(project)
        await session.flush()
        for index, user in enumerate((lead, *members)):
            is_lead = user.id == lead.id
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event_id,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="create" if is_lead else "invite",
                    is_team_row=team_status is not None,
                    joined_at=h.now - timedelta(days=10 - index),
                    confirmed_at=h.now - timedelta(days=10 - index),
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def add_member(
    h: Harness,
    project: models.Project,
    user: models.User,
    *,
    joined_days_ago: int,
    state: str = "confirmed",
    left: bool = False,
    banned: bool = False,
) -> models.ProjectMember:
    async with h.db() as session:
        if banned:
            await session.execute(
                update(models.User).where(models.User.id == user.id).values(banned_at=h.now)
            )
        row = models.ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role="member",
            state=state,
            trust="self",
            joined_via="invite",
            is_team_row=False,
            joined_at=h.now - timedelta(days=joined_days_ago),
            confirmed_at=None if state == "proposed" else h.now - timedelta(days=joined_days_ago),
            accept_by=h.now + timedelta(days=1) if state == "proposed" else None,
            left_at=h.now if left else None,
            left_reason="left" if left else None,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def member_row(
    h: Harness, project: models.Project, user: models.User
) -> models.ProjectMember:
    async with h.db() as session:
        return (
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.user_id == user.id,
                )
            )
        ).one()


async def stored_project(h: Harness, project: models.Project) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project.id)


async def test_lead_leave_passes_to_earliest_confirmed(harness: Harness) -> None:
    h = harness
    lead, early, late = await h.user(), await h.user(), await h.user()
    left, proposed, banned = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=(early, late))
    await add_member(h, project, left, joined_days_ago=20, left=True)
    await add_member(h, project, proposed, joined_days_ago=30, state="proposed")
    await add_member(h, project, banned, joined_days_ago=40, banned=True)
    async with await h.client(lead) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    row = await stored_project(h, project)
    assert row.lead_user_id == early.id
    assert (await member_row(h, project, early)).role == "lead"
    assert (await member_row(h, project, lead)).left_reason == "left"
    async with h.db() as session:
        notices = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "lead_assigned")
            )
        )
        assert [notice.user_id for notice in notices] == [early.id]
        assert isinstance(notices[0].payload, dict)
        assert notices[0].payload["lead"] == {
            "user_id": str(early.id),
            "display_name": early.display_name,
        }


async def test_lead_leave_without_members_pauses(harness: Harness) -> None:
    h = harness
    lead = await h.user()
    project = await seed_project(h, lead, status="active")
    async with await h.client(lead) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/leave")
    assert response.status_code == 204, response.text
    row = await stored_project(h, project)
    assert row.lead_user_id is None
    assert row.status == "paused"
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0


async def test_transfer_lead_is_immediate(harness: Harness) -> None:
    h = harness
    lead, target, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=(target, other))
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/transfer-lead", json={"user_id": str(target.id)}
        )
    assert response.status_code == 200, response.text
    assert response.json()["lead_user_id"] == str(target.id)
    row = await stored_project(h, project)
    assert row.lead_user_id == target.id
    assert (await member_row(h, project, target)).role == "lead"
    assert (await member_row(h, project, lead)).role == "member"
    async with h.db() as session:
        [notice] = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "lead_assigned")
            )
        )
        assert notice.user_id == target.id
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "lead_assign")
            )
        )
        assert audits[-1].actor_id == lead.id
        assert audits[-1].after == {"lead_user_id": str(target.id)}


async def test_transfer_lead_requires_confirmed_member(harness: Harness) -> None:
    h = harness
    lead, proposed = await h.user(), await h.user()
    project = await seed_project(h, lead)
    await add_member(h, project, proposed, joined_days_ago=1, state="proposed")
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{project.id}/transfer-lead",
                json={"user_id": str(proposed.id)},
            ),
            403,
            ErrorCode.FORBIDDEN,
        )
    assert (await stored_project(h, project)).lead_user_id == lead.id


async def test_transfer_lead_respects_led_limit(harness: Harness) -> None:
    h = harness
    lead, target = await h.user(), await h.user()
    project = await seed_project(h, lead, members=(target,))
    for _ in range(5):
        await seed_project(h, target)
    async with await h.client(lead) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{project.id}/transfer-lead", json={"user_id": str(target.id)}
            ),
            409,
            ErrorCode.PROJECTS_LED_LIMIT,
        )
    assert (await stored_project(h, project)).lead_user_id == lead.id


async def test_transfer_lead_forbidden_for_member(harness: Harness) -> None:
    h = harness
    lead, member, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=(member, other))
    async with await h.client(member) as client:
        assert_problem(
            await client.post(
                f"/api/v1/projects/{project.id}/transfer-lead", json={"user_id": str(other.id)}
            ),
            403,
            ErrorCode.FORBIDDEN,
        )


async def test_ban_lead_passes_role_and_keeps_history(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    lead, heir, other = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, lead, members=(heir, other), status="active")
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{lead.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    row = await stored_project(h, project)
    assert row.lead_user_id == heir.id
    removed = await member_row(h, project, lead)
    assert removed.left_at == h.now
    assert removed.left_reason == "banned"
    async with h.db() as session:
        [audit] = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "lead_step_down")
            )
        )
        assert audit.actor_id == admin.id
        assert audit.actor_kind == "admin"
        assert audit.reason == "banned"
        left = list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "project_member_left")
            )
        )
        assert {notice.user_id for notice in left} == {heir.id, other.id}
        assert not [
            notice
            for notice in await session.scalars(select(models.Notification))
            if notice.user_id == lead.id
        ]


async def test_ban_member_keeps_lead(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    lead, member = await h.user(), await h.user()
    project = await seed_project(h, lead, members=(member,))
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/users/{member.id}/ban", json={"reason": "fraud"}
        )
    assert response.status_code == 200, response.text
    assert (await stored_project(h, project)).lead_user_id == lead.id
    assert (await member_row(h, project, member)).left_reason == "banned"


async def test_ban_sole_lead_pauses_project(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    lead = await h.user()
    project = await seed_project(h, lead)
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{lead.id}/ban", json={"reason": "fraud"})
    assert response.status_code == 200, response.text
    row = await stored_project(h, project)
    assert row.lead_user_id is None
    assert row.status == "paused"


@pytest.mark.parametrize("team_status", ["finished", "withdrawn"])
async def test_transfer_lead_after_result(harness: Harness, team_status: str) -> None:
    h = harness
    await h.seed_areas()
    lead, target = await h.user(), await h.user()
    event = await make_event(h)
    project = await seed_project(
        h, lead, members=(target,), team_status=team_status, event_id=event.id
    )
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/transfer-lead", json={"user_id": str(target.id)}
        )
    assert response.status_code == 200, response.text
    assert (await stored_project(h, project)).lead_user_id == target.id
