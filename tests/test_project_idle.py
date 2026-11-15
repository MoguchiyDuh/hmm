from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from harness import NOW, Harness
from sqlalchemy import select

from hmm import models
from hmm.projects import service
from hmm.projects.idle import mark_project_progress


@pytest.mark.parametrize(
    ("status", "paused_reason", "expected_status", "expected_reason"),
    [
        ("idea", None, "idea", None),
        ("paused", "idle", "active", None),
        ("paused", "manual", "paused", "manual"),
    ],
)
def test_mark_progress_without_starting_idea(
    status: str,
    paused_reason: str | None,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    project = models.Project(status=status, paused_reason=paused_reason, team_status=None)
    mark_project_progress(project, NOW, start_idea=False)
    assert project.last_progress_at == NOW
    assert project.status == expected_status
    assert project.paused_reason == expected_reason


async def seed_project(
    h: Harness,
    lead: models.User,
    *,
    members: Sequence[models.User] = (),
    status: str = "idea",
    team_status: str | None = None,
    event_id: UUID | None = None,
    last_progress_at: datetime | None = None,
    visibility: str = "public",
    paused_reason: str | None = None,
) -> models.Project:
    async with h.db() as session:
        if team_status is not None and event_id is None:
            event = models.Event(
                slug=f"idle-{uuid4()}",
                kind="hackathon",
                formation_enabled=False,
                title="Hackathon",
                starts_at=h.now,
                ends_at=h.now + timedelta(days=2),
                timezone="UTC",
                language=[],
                themes=[],
                status="published",
                review_reasons=[],
                origin="manual",
                unmapped_areas=[],
            )
            session.add(event)
            await session.flush()
            event_id = event.id
        project = models.Project(
            event_id=event_id,
            name="Project",
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=None,
            lead_user_id=lead.id,
            status=status,
            team_status=team_status,
            paused_reason=paused_reason,
            origin="standalone",
            last_progress_at=last_progress_at or h.now,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    user_id=user.id,
                    role="lead" if user.id == lead.id else "member",
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


async def test_idle_warning_boundary_and_dedupe(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    await seed_project(
        h,
        lead,
        members=[member],
        last_progress_at=h.now - timedelta(days=30) + timedelta(seconds=1),
    )
    project = await seed_project(
        h, lead, members=[member], last_progress_at=h.now - timedelta(days=30)
    )
    async with h.db() as session:
        assert await service.warn_idle_projects(session, h.now) == 1
        assert await service.warn_idle_projects(session, h.now) == 0
        [notification] = list(await session.scalars(select(models.Notification)))
        assert notification.user_id == lead.id
        assert notification.type == "project_idle"
        assert isinstance(notification.payload, dict)
        assert notification.payload["project_id"] == str(project.id)
        assert notification.payload["idle_days"] == 30
        pauses_at = notification.payload["pauses_at"]
        assert isinstance(pauses_at, str)
        assert datetime.fromisoformat(pauses_at) == h.now + timedelta(days=30)


@pytest.mark.parametrize("pause", [False, True])
async def test_idle_timer_scope(harness: Harness, pause: bool) -> None:
    h = harness
    lead = await h.user()
    old = h.now - timedelta(days=65)
    for status in ("paused", "shipped", "abandoned", "disbanded", "cancelled"):
        await seed_project(h, lead, status=status, last_progress_at=old)
    leaderless = await seed_project(h, lead, last_progress_at=old)
    missing_progress = await seed_project(h, lead, last_progress_at=old)
    async with h.db() as session:
        row = await session.get(models.Project, leaderless.id)
        assert row is not None
        row.lead_user_id = None
        row = await session.get(models.Project, missing_progress.id)
        assert row is not None
        row.last_progress_at = None
    for team_status in ("formed", "active", "disbanded", "cancelled"):
        await seed_project(h, lead, team_status=team_status, last_progress_at=old)
    finished = await seed_project(
        h, lead, status="active", team_status="finished", last_progress_at=old
    )
    async with h.db() as session:
        timer = service.pause_idle_projects if pause else service.warn_idle_projects
        assert await timer(session, h.now) == 1
        [notification] = list(await session.scalars(select(models.Notification)))
        assert isinstance(notification.payload, dict)
        assert notification.payload["project_id"] == str(finished.id)
        assert notification.type == ("project_paused" if pause else "project_idle")


async def test_idle_pause_boundary_and_effects(harness: Harness) -> None:
    h = harness
    lead, member = await h.user(), await h.user()
    recent = await seed_project(
        h,
        lead,
        members=[member],
        last_progress_at=h.now - timedelta(days=60) + timedelta(seconds=1),
    )
    project = await seed_project(
        h, lead, members=[member], last_progress_at=h.now - timedelta(days=60)
    )
    async with h.db() as session:
        item = models.FeedItem(
            kind="project_recruiting",
            subject_type="project",
            subject_id=project.id,
            actor_id=lead.id,
            area_ids=[],
            payload={},
        )
        session.add(item)
        await session.flush()
        item_id = item.id
    async with h.db() as session:
        assert await service.pause_idle_projects(session, h.now) == 1
        assert await service.pause_idle_projects(session, h.now) == 0
    async with h.db() as session:
        row = await session.get(models.Project, project.id)
        assert row is not None
        assert row.status == "paused"
        assert row.paused_reason == "idle"
        assert row.last_progress_at == project.last_progress_at
        row = await session.get(models.Project, recent.id)
        assert row is not None and row.status == "idea"
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert {notification.user_id for notification in notifications} == {lead.id, member.id}
        for notification in notifications:
            assert notification.type == "project_paused"
            assert isinstance(notification.payload, dict)
            assert notification.payload["status"] == "paused"
            assert notification.payload["reason"]
        [audit] = list(await session.scalars(select(models.AuditLog)))
        assert audit.action == "system_transition"
        assert audit.actor_id is None
        assert audit.actor_kind == "system"
        assert audit.entity_type == "project"
        assert audit.entity_id == str(project.id)
        assert audit.before == {"status": "idea"}
        assert audit.after == {"status": "paused", "paused_reason": "idle"}
        assert audit.reason == "Project inactive"
        item = await session.get(models.FeedItem, item_id)
        assert item is not None and item.hidden_at == h.now


async def test_manual_idle_and_leaderless_pause_causes(harness: Harness) -> None:
    h = harness
    lead = await h.user()
    project = await seed_project(h, lead)
    async with h.db() as session:
        await service.pause_project(session, project.id, lead, h.now)
        row = await session.get(models.Project, project.id)
        assert row is not None
        assert row.paused_reason == "manual"
        assert service.resume_idle_project(row) is False
        assert row.status == "paused" and row.paused_reason == "manual"
        await service.start_project(session, project.id, lead, h.now)
        assert row.status == "active" and row.paused_reason is None
        row.status = "paused"
        row.paused_reason = "idle"
        assert service.resume_idle_project(row) is True
        assert row.status == "active" and row.paused_reason is None


@pytest.mark.parametrize(
    ("status", "paused_reason", "team_status", "expected_status", "expected_reason"),
    [
        ("idea", None, None, "active", None),
        ("paused", "idle", None, "active", None),
        ("paused", "manual", None, "paused", "manual"),
        ("paused", None, None, "paused", None),
        ("idea", None, "formed", "idea", None),
        ("active", None, None, "active", None),
    ],
)
async def test_mark_project_progress(
    harness: Harness,
    status: str,
    paused_reason: str | None,
    team_status: str | None,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    h = harness
    lead = await h.user()
    project = await seed_project(
        h,
        lead,
        status=status,
        paused_reason=paused_reason,
        team_status=team_status,
        last_progress_at=h.now - timedelta(days=1),
    )
    async with h.db() as session:
        row = await session.get(models.Project, project.id, with_for_update=True)
        assert row is not None
        service.mark_project_progress(row, h.now)
        assert row.last_progress_at == h.now
        assert row.status == expected_status
        assert row.paused_reason == expected_reason


async def test_run_project_timers(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        await service.run_project_timers(session, h.now)
    lead = await h.user()
    project = await seed_project(h, lead, last_progress_at=h.now - timedelta(days=65))
    async with h.db() as session:
        await service.run_project_timers(session, h.now)
    async with h.db() as session:
        await service.run_project_timers(session, h.now)
        row = await session.get(models.Project, project.id)
        assert row is not None and row.status == "paused" and row.paused_reason == "idle"
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert {notification.type for notification in notifications} == {
            "project_idle",
            "project_paused",
        }
        assert {notification.user_id for notification in notifications} == {lead.id}


@pytest.mark.parametrize("timer", ["warn", "pause", "both"])
async def test_idle_timer_bounded_backlog(harness: Harness, timer: str) -> None:
    h = harness
    lead = await h.user()
    old = h.now - timedelta(days=65) + timedelta(microseconds=900_000)
    projects = [await seed_project(h, lead, last_progress_at=old) for _ in range(2)]
    for index in range(2):
        async with h.db() as session:
            if timer == "both":
                await service.run_project_timers(session, h.now, limit=1)
            elif timer == "warn":
                assert await service.warn_idle_projects(session, h.now, limit=1) == 1
            else:
                assert await service.pause_idle_projects(session, h.now, limit=1) == 1
            notifications = list(await session.scalars(select(models.Notification)))
            expected_count = (index + 1) * (2 if timer == "both" else 1)
            assert len(notifications) == expected_count
            assert {
                notification.payload["project_id"]
                for notification in notifications
                if isinstance(notification.payload, dict)
            } == {str(project.id) for project in projects[: index + 1]}
    async with h.db() as session:
        assert await service.warn_idle_projects(session, h.now, limit=1) == 0
        if timer != "warn":
            assert await service.pause_idle_projects(session, h.now, limit=1) == 0
        else:
            row = await session.get(models.Project, projects[0].id, with_for_update=True)
            assert row is not None
            row.last_progress_at = old + timedelta(days=1)
            await session.flush()
            assert await service.warn_idle_projects(session, h.now, limit=1) == 1
