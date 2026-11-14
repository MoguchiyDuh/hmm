from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import AuditAction, ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


@dataclass
class Team:
    lead: models.User
    a: models.User
    b: models.User
    outsider: models.User
    admin: models.User
    event: models.Event
    project: models.Project


@pytest.fixture
async def team(harness: Harness) -> Team:
    await harness.seed_areas()
    lead = await harness.user(login="lead")
    a = await harness.user(login="alice")
    b = await harness.user(login="bob")
    outsider = await harness.user(login="outsider")
    admin = await harness.admin()
    async with harness.db() as session:
        event = models.Event(
            slug="nl-2026",
            kind="hackathon",
            formation_enabled=True,
            title="NL 2026",
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
        session.add(event)
        await session.flush()
        project = models.Project(
            name="Aurora",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            team_status="active",
            origin="claim",
            event_id=event.id,
            lead_user_id=lead.id,
        )
        session.add(project)
        await session.flush()
        for user, role, via in (
            (lead, "lead", "claim"),
            (a, "member", "formation"),
            (b, "member", "formation"),
        ):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role=role,
                    state="confirmed",
                    trust="self",
                    joined_via=via,
                    is_team_row=True,
                    joined_at=harness.now - timedelta(days=2),
                )
            )
    return Team(lead, a, b, outsider, admin, event, project)


def participants(team: Team, lead: bool, a: bool, b: bool) -> dict[str, Any]:
    return {
        "participants": {str(team.lead.id): lead, str(team.a.id): a, str(team.b.id): b},
        "member_areas": {
            str(team.lead.id): "backend",
            str(team.a.id): "frontend",
            str(team.b.id): "backend",
        },
    }


async def submit(harness: Harness, team: Team, **overrides: Any) -> None:
    body: dict[str, Any] = {
        "outcome": "finished",
        "place": 2,
        "award": "2nd place",
        "repo_url": "https://github.com/a/b",
        "retrospective_md": "We shipped it.",
        **participants(team, True, True, False),
        **overrides,
    }
    async with await harness.client(team.lead) as c:
        response = await c.post(f"/api/v1/projects/{team.project.id}/result", json=body)
        assert response.status_code == 201, response.text
    await harness.run_jobs()


async def patch(harness: Harness, user: models.User, team: Team, **body: Any) -> httpx.Response:
    async with await harness.client(user) as c:
        return await c.patch(f"/api/v1/projects/{team.project.id}/result", json=body)


async def rows(harness: Harness, model: type[models.Base], *where: Any) -> list[Any]:
    async with harness.db() as session:
        return list(await session.scalars(select(model).where(*where)))


async def count(harness: Harness, model: type[models.Base], *where: Any) -> int:
    async with harness.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def live_system(harness: Harness, user: models.User) -> list[str]:
    confirmations = await rows(
        harness,
        models.Confirmation,
        models.Confirmation.subject_user_id == user.id,
        models.Confirmation.source == "system",
        models.Confirmation.withdrawn_at.is_(None),
    )
    return sorted(row.fact_type for row in confirmations)


async def edits(harness: Harness) -> list[models.AuditLog]:
    return await rows(harness, models.AuditLog, models.AuditLog.action == AuditAction.RESULT_EDIT)


async def todo_recomputes(harness: Harness) -> set[str]:
    return {
        job["args"]["user_id"]
        for job in await harness.pending_jobs("recompute_member")
        if job["status"] == "todo"
    }


async def load_result(harness: Harness, team: Team) -> models.ProjectResult:
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, team.project.id)
        assert result is not None
        return result


async def load_project(harness: Harness, team: Team) -> models.Project:
    async with harness.db() as session:
        project = await session.get(models.Project, team.project.id)
        assert project is not None
        return project


async def test_scalar_edit(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    [release] = await rows(harness, models.ProjectRelease)
    response = await patch(
        harness,
        team.lead,
        team,
        award="Best UX",
        retrospective_md="new",
        repo_url="https://github.com/a/c",
    )
    assert response.status_code == 200, response.text
    assert "edit" in response.json()["allowed_actions"]
    result = await load_result(harness, team)
    assert result.edited_at == harness.now
    assert result.award == "Best UX"
    assert result.retrospective_md == "new"
    [updated] = await rows(harness, models.ProjectRelease)
    assert updated.id == release.id
    assert updated.repo_url == "https://github.com/a/c"
    assert updated.notes_md == "Best UX"
    assert await count(harness, models.ReviewWindow) == 1
    assert await count(harness, models.EventAttendance) == 2
    assert (
        await count(
            harness,
            models.Confirmation,
            models.Confirmation.source == "system",
            models.Confirmation.withdrawn_at.is_(None),
        )
        == 4
    )
    assert len(await edits(harness)) == 1


async def test_member_cannot_change_participants_or_outcome(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    before = await load_result(harness, team)
    assert_problem(
        await patch(harness, team.lead, team, **participants(team, True, False, False)),
        409,
        ErrorCode.INVALID_STATE,
    )
    assert_problem(
        await patch(harness, team.lead, team, outcome="withdrawn"), 409, ErrorCode.INVALID_STATE
    )
    after = await load_result(harness, team)
    assert after.participants == before.participants
    assert after.outcome == "finished"
    assert after.edited_at is None
    assert await edits(harness) == []


async def test_admin_removes_participant(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    response = await patch(harness, team.admin, team, **participants(team, True, False, False))
    assert response.status_code == 200, response.text
    assert await live_system(harness, team.a) == []
    assert await live_system(harness, team.lead) == ["attendance", "project_member"]
    async with harness.db() as session:
        member = await session.scalar(
            select(models.ProjectMember).where(models.ProjectMember.user_id == team.a.id)
        )
        attendance = await session.scalar(
            select(models.EventAttendance).where(models.EventAttendance.user_id == team.a.id)
        )
        assert member is not None
        assert attendance is not None
        assert member.trust == "self"
        assert attendance.trust == "self"
        assert attendance.status == "attended"
    assert await todo_recomputes(harness) == {str(team.lead.id), str(team.a.id), str(team.b.id)}
    [audit] = await edits(harness)
    assert isinstance(audit.before, dict)
    assert isinstance(audit.after, dict)
    assert audit.before["participants"] == {
        str(team.lead.id): True,
        str(team.a.id): True,
        str(team.b.id): False,
    }
    assert audit.after["participants"] == {
        str(team.lead.id): True,
        str(team.a.id): False,
        str(team.b.id): False,
    }


async def test_admin_adds_participant(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    [window] = await rows(harness, models.ReviewWindow)
    notifications = await count(
        harness, models.Notification, models.Notification.type == "reviews_open"
    )
    response = await patch(harness, team.admin, team, **participants(team, True, True, True))
    assert response.status_code == 200, response.text
    attendances = await rows(
        harness, models.EventAttendance, models.EventAttendance.user_id == team.b.id
    )
    assert [row.status for row in attendances] == ["attended"]
    assert await live_system(harness, team.b) == ["attendance", "project_member"]
    [same] = await rows(harness, models.ReviewWindow)
    assert same.id == window.id
    assert same.participants == window.participants
    assert (
        await count(harness, models.Notification, models.Notification.type == "reviews_open")
        == notifications
    )
    jobs = [
        job
        for job in await harness.pending_jobs("recompute_member")
        if job["status"] == "todo" and job["args"]["user_id"] == str(team.b.id)
    ]
    assert len(jobs) == 1


async def test_admin_flips_finished_to_withdrawn(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    [window] = await rows(harness, models.ReviewWindow)
    confirmations = {row.id for row in await rows(harness, models.Confirmation)}
    response = await patch(harness, team.admin, team, outcome="withdrawn")
    assert response.status_code == 200, response.text
    project = await load_project(harness, team)
    assert project.team_status == "withdrawn"
    assert project.status == "abandoned"
    assert await count(harness, models.ProjectRelease) == 1
    [same] = await rows(harness, models.ReviewWindow)
    assert (same.id, same.closes_at, same.participants) == (
        window.id,
        window.closes_at,
        window.participants,
    )
    assert {row.id for row in await rows(harness, models.Confirmation)} == confirmations


async def test_edit_permissions(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    assert_problem(await patch(harness, team.a, team, award="x"), 403, ErrorCode.FORBIDDEN)
    assert_problem(await patch(harness, team.outsider, team, award="x"), 403, ErrorCode.FORBIDDEN)
    notifications = await count(
        harness, models.Notification, models.Notification.type == "reviews_open"
    )
    harness.advance(timedelta(days=8))
    assert_problem(await patch(harness, team.lead, team, award="x"), 409, ErrorCode.INVALID_STATE)
    response = await patch(harness, team.admin, team, award="x")
    assert response.status_code == 200, response.text
    assert response.json()["award"] == "x"
    assert await count(harness, models.ReviewWindow) == 1
    assert (
        await count(harness, models.Notification, models.Notification.type == "reviews_open")
        == notifications
    )


async def test_withdrawn_result_edits(harness: Harness, team: Team) -> None:
    await submit(harness, team, outcome="withdrawn")
    assert_problem(
        await patch(harness, team.lead, team, retrospective_md="x"), 409, ErrorCode.INVALID_STATE
    )
    response = await patch(harness, team.admin, team, retrospective_md="admin")
    assert response.status_code == 200, response.text
    project = await load_project(harness, team)
    assert (project.team_status, project.status) == ("withdrawn", "abandoned")
    assert await count(harness, models.ProjectRelease) == 0
    response = await patch(harness, team.admin, team, outcome="finished")
    assert response.status_code == 200, response.text
    project = await load_project(harness, team)
    assert (project.team_status, project.status) == ("finished", "shipped")
    assert await count(harness, models.ProjectRelease) == 1
    assert await live_system(harness, team.lead) == ["attendance", "project_member"]
    assert await live_system(harness, team.a) == ["attendance", "project_member"]
    assert await live_system(harness, team.b) == []
    assert await count(harness, models.ReviewWindow) == 1


async def test_place_reverification(harness: Harness, team: Team) -> None:
    await submit(harness, team)
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, team.project.id)
        assert result is not None
        result.place_status = "verified"
        result.place_verified_by = team.admin.id
        result.place_verified_at = harness.now
    response = await patch(harness, team.lead, team, retrospective_md="later")
    assert response.status_code == 200, response.text
    result = await load_result(harness, team)
    assert result.place_status == "verified"
    assert result.place_verified_by == team.admin.id
    assert result.place_verified_at == harness.now
    response = await patch(harness, team.lead, team, place=4)
    assert response.status_code == 200, response.text
    result = await load_result(harness, team)
    assert result.place == 4
    assert result.place_status == "unverified"
    assert result.place_verified_by is None
    assert result.place_verified_at is None
    response = await patch(harness, team.admin, team, place=None, award=None)
    assert response.status_code == 200, response.text
    result = await load_result(harness, team)
    assert result.place is None
    assert result.award is None
    assert result.place_status is None
