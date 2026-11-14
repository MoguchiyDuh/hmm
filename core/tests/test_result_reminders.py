from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import AuditAction, AuditEntityType
from hmm_contracts.notifications import ResultDuePayload
from hmm_core import models
from hmm_core.results.service import (
    auto_close_result_teams,
    run_result_timers,
    send_result_due,
)
from sqlalchemy import func, select


@dataclass
class Team:
    project: models.Project
    event: models.Event
    lead: models.User
    members: list[models.User]
    left: models.User

    @property
    def team(self) -> set[UUID]:
        return {self.lead.id, *(member.id for member in self.members)}


async def seed_team(
    harness: Harness, *, ends_at: datetime | None = None, team_status: str = "active"
) -> Team:
    lead = await harness.user()
    members = [await harness.user(), await harness.user()]
    left = await harness.user()
    end = ends_at or harness.now - timedelta(days=1)
    async with harness.db() as session:
        event = models.Event(
            slug=str(uuid4()),
            kind="hackathon",
            formation_enabled=True,
            title="NL 2026",
            starts_at=end - timedelta(days=2),
            ends_at=end,
            timezone="UTC",
            language=["en"],
            themes=[],
            status="published",
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        project = models.Project(
            name=f"Team {uuid4().hex[:6]}",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            team_status=team_status,
            origin="claim",
            event_id=event.id,
            lead_user_id=lead.id,
        )
        session.add(project)
        await session.flush()
        for user, role in ((lead, "lead"), *((member, "member") for member in members)):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role=role,
                    state="confirmed",
                    trust="self",
                    joined_via="formation",
                    is_team_row=True,
                    joined_at=end - timedelta(days=2),
                )
            )
        session.add(
            models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=left.id,
                role="member",
                state="confirmed",
                trust="self",
                joined_via="formation",
                is_team_row=True,
                joined_at=end - timedelta(days=2),
                left_at=end - timedelta(days=1),
            )
        )
    return Team(project, event, lead, members, left)


async def notices(harness: Harness) -> list[models.Notification]:
    async with harness.db() as session:
        return list(
            await session.scalars(
                select(models.Notification)
                .where(models.Notification.type == "result_due")
                .order_by(models.Notification.created_at, models.Notification.id)
            )
        )


async def alerts(harness: Harness) -> list[models.AdminAlert]:
    async with harness.db() as session:
        return list(await session.scalars(select(models.AdminAlert)))


async def due(harness: Harness, *, limit: int = 500) -> int:
    async with harness.db() as session:
        return await send_result_due(session, harness.now, limit=limit)


async def close(harness: Harness, *, limit: int = 500) -> int:
    async with harness.db() as session:
        return await auto_close_result_teams(session, harness.jobs, harness.now, limit=limit)


async def count(harness: Harness, model: type[models.Base]) -> int:
    async with harness.db() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def load_project(harness: Harness, project_id: UUID) -> models.Project:
    async with harness.db() as session:
        project = await session.get(models.Project, project_id)
        assert project is not None
        return project


async def test_lead_notice_after_end(harness: Harness) -> None:
    team = await seed_team(harness)
    assert await due(harness) == 1
    [notice] = await notices(harness)
    assert notice.user_id == team.lead.id
    ends_at = team.event.ends_at
    assert ResultDuePayload.model_validate(notice.payload) == ResultDuePayload(
        link=f"/projects/{team.project.id}",
        actions=(),
        event_id=team.event.id,
        event_title="NL 2026",
        project_id=team.project.id,
        team_name=team.project.name,
        ends_at=ends_at,
        member_submit_at=ends_at + timedelta(days=7),
        closes_at=ends_at + timedelta(days=30),
    )
    assert await alerts(harness) == []
    assert await due(harness) == 0


async def test_member_phase_and_alert(harness: Harness) -> None:
    team = await seed_team(harness)
    assert await due(harness) == 1
    harness.advance(timedelta(days=6))
    assert await due(harness) == 2
    rows = await notices(harness)
    assert sorted(row.user_id for row in rows) == sorted(team.team)
    assert team.left.id not in {row.user_id for row in rows}
    [alert] = await alerts(harness)
    assert alert.severity == "warning"
    assert alert.type == "result_overdue"
    assert alert.status == "open"
    assert alert.subject_id == team.project.id
    assert alert.dedupe_key == f"result_overdue:{team.project.id}"
    assert await due(harness) == 0
    assert len(await alerts(harness)) == 1


async def test_reminders_every_three_days(harness: Harness) -> None:
    team = await seed_team(harness)
    await due(harness)
    harness.advance(timedelta(days=6))
    await due(harness)
    harness.advance(timedelta(days=3))
    assert await due(harness) == 3
    keys = {row.dedupe_key for row in await notices(harness)}
    assert {f"result_due_reminder:{team.project.id}:{uid}:1" for uid in team.team} <= keys
    harness.advance(timedelta(days=1))
    assert await due(harness) == 0
    harness.advance(timedelta(days=2))
    assert await due(harness) == 3
    keys = {row.dedupe_key for row in await notices(harness)}
    assert {f"result_due_reminder:{team.project.id}:{uid}:2" for uid in team.team} <= keys


async def test_auto_close(harness: Harness) -> None:
    team = await seed_team(harness, ends_at=harness.now - timedelta(days=30))
    assert await close(harness) == 1
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, team.project.id)
        assert result is not None
        assert result.outcome == "withdrawn"
        assert result.auto_closed is True
        assert result.participants == {}
        assert result.member_areas == {}
        assert result.place_status is None
        assert result.submitted_by is None
        assert result.submitted_at is None
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == AuditAction.SYSTEM_TRANSITION,
                    models.AuditLog.entity_type == AuditEntityType.PROJECT_RESULT,
                )
            )
        )
    assert len(audits) == 1
    assert audits[0].entity_id == str(team.project.id)
    project = await load_project(harness, team.project.id)
    assert (project.team_status, project.status) == ("withdrawn", "abandoned")
    jobs = await harness.pending_jobs("recompute_member")
    assert sorted(job["args"]["user_id"] for job in jobs) == sorted(str(uid) for uid in team.team)
    assert await count(harness, models.Confirmation) == 0
    assert await count(harness, models.EventAttendance) == 0
    assert await count(harness, models.ReviewWindow) == 0
    assert await count(harness, models.Notification) == 0
    assert await close(harness) == 0


async def test_auto_close_boundary(harness: Harness) -> None:
    await seed_team(harness, ends_at=harness.now - timedelta(days=30) + timedelta(hours=1))
    assert await close(harness) == 0
    harness.advance(timedelta(hours=1))
    assert await close(harness) == 1


async def test_skips_teams_with_results_or_inactive(harness: Harness) -> None:
    team = await seed_team(harness, ends_at=harness.now - timedelta(days=30))
    async with harness.db() as session:
        session.add(
            models.ProjectResult(
                project_id=team.project.id,
                outcome="finished",
                member_areas={},
                participants={},
                auto_closed=False,
            )
        )
    finished = await seed_team(
        harness, ends_at=harness.now - timedelta(days=30), team_status="finished"
    )
    withdrawn = await seed_team(
        harness, ends_at=harness.now - timedelta(days=30), team_status="withdrawn"
    )
    assert await due(harness) == 0
    assert await close(harness) == 0
    assert await notices(harness) == []
    assert await alerts(harness) == []
    for current, status in ((team, "active"), (finished, "finished"), (withdrawn, "withdrawn")):
        assert (await load_project(harness, current.project.id)).team_status == status


async def notified_projects(harness: Harness) -> set[UUID]:
    return {
        ResultDuePayload.model_validate(row.payload).project_id for row in await notices(harness)
    }


async def test_limit(harness: Harness) -> None:
    first = await seed_team(harness)
    second = await seed_team(harness)
    assert await due(harness, limit=1) == 1
    assert await notified_projects(harness) == {min(first.project.id, second.project.id)}
    assert await due(harness, limit=1) == 1
    assert await notified_projects(harness) == {first.project.id, second.project.id}
    harness.advance(timedelta(days=29))
    assert await close(harness, limit=1) == 1
    assert await close(harness, limit=1) == 1
    assert await close(harness, limit=1) == 0
    for current in (first, second):
        assert (await load_project(harness, current.project.id)).team_status == "withdrawn"


async def test_run_result_timers(harness: Harness) -> None:
    team = await seed_team(harness, ends_at=harness.now - timedelta(days=30))
    closed = await seed_team(
        harness, ends_at=harness.now - timedelta(days=30), team_status="withdrawn"
    )
    async with harness.db() as session:
        session.add(
            models.ProjectResult(
                project_id=closed.project.id,
                outcome="withdrawn",
                member_areas={},
                participants={},
                auto_closed=True,
            )
        )
    async with harness.db() as session:
        await run_result_timers(session, harness.jobs, harness.now)
    rows = await notices(harness)
    assert len(rows) == 6
    assert {row.user_id for row in rows} == team.team
    [alert] = await alerts(harness)
    assert alert.dedupe_key == f"result_overdue:{team.project.id}"
    assert alert.status == "resolved"
    assert alert.resolution_note == "Auto-closed without a result"
    project = await load_project(harness, team.project.id)
    assert (project.team_status, project.status) == ("withdrawn", "abandoned")
    async with harness.db() as session:
        await run_result_timers(session, harness.jobs, harness.now)
    assert len(await notices(harness)) == 6
    assert len(await alerts(harness)) == 1
    assert await count(harness, models.ProjectResult) == 2
