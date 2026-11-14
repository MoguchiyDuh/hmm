from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import AuditAction, AuditEntityType, ErrorCode
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


def result_body(team: Team, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "outcome": "finished",
        "place": 2,
        "award": "2nd place",
        "repo_url": "https://github.com/a/b",
        "participants": {str(team.lead.id): True, str(team.a.id): True, str(team.b.id): False},
        "member_areas": {
            str(team.lead.id): "backend",
            str(team.a.id): "frontend",
            str(team.b.id): "backend",
        },
        "retrospective_md": "We shipped it.",
    }
    body.update(overrides)
    return body


async def submit(harness: Harness, user: models.User, team: Team, **overrides: Any) -> Any:
    async with await harness.client(user) as c:
        return await c.post(
            f"/api/v1/projects/{team.project.id}/result", json=result_body(team, **overrides)
        )


async def rows(harness: Harness, model: type[models.Base], *where: Any) -> list[Any]:
    async with harness.db() as session:
        return list(await session.scalars(select(model).where(*where)))


async def count(harness: Harness, model: type[models.Base]) -> int:
    async with harness.db() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_lead_submits_finished_result(harness: Harness, team: Team) -> None:
    response = await submit(harness, team.lead, team)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["place_status"] == "unverified"
    assert data["retrospective_md"] == "We shipped it."
    assert data["allowed_actions"] == ["edit"]
    assert len(await harness.pending_jobs("recompute_member")) == 3
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, team.project.id)
        project = await session.get(models.Project, team.project.id)
        assert result is not None
        assert project is not None
        assert result.submitted_by == team.lead.id
        assert result.submitted_at == harness.now
        assert result.auto_closed is False
        assert result.place == 2
        assert result.place_status == "unverified"
        assert project.team_status == "finished"
        assert project.status == "shipped"
    releases = await rows(harness, models.ProjectRelease)
    assert len(releases) == 1
    release = releases[0]
    assert release.title == "Hackathon result: NL 2026"
    assert release.notes_md == "2nd place"
    assert release.repo_url == "https://github.com/a/b"
    assert release.demo_url is None
    assert release.writeup_url is None
    assert release.created_by == team.lead.id
    assert await count(harness, models.ReleaseContribution) == 0
    await harness.run_jobs()
    attendances = await rows(harness, models.EventAttendance)
    assert {row.user_id for row in attendances} == {team.lead.id, team.a.id}
    for row in attendances:
        assert row.status == "attended"
        assert row.role == "attendee"
        assert row.trust == "trusted"
    confirmations = await rows(
        harness, models.Confirmation, models.Confirmation.withdrawn_at.is_(None)
    )
    assert len(confirmations) == 4
    assert sorted(row.fact_type for row in confirmations) == [
        "attendance",
        "attendance",
        "project_member",
        "project_member",
    ]
    assert all(row.confirmer_id is None and row.source == "system" for row in confirmations)
    assert {row.subject_user_id for row in confirmations} == {team.lead.id, team.a.id}
    windows = await rows(harness, models.ReviewWindow)
    assert len(windows) == 1
    assert windows[0].participants == {str(team.lead.id): "lead", str(team.a.id): "member"}
    assert windows[0].closes_at == harness.now + timedelta(days=7)
    notifications = await rows(
        harness, models.Notification, models.Notification.type == "reviews_open"
    )
    assert {row.user_id for row in notifications} == {team.lead.id, team.a.id}
    assert len(notifications) == 2
    feed = await rows(harness, models.FeedItem, models.FeedItem.kind == "project_shipped")
    assert len(feed) == 1
    audits = await rows(
        harness, models.AuditLog, models.AuditLog.action == AuditAction.RESULT_SUBMIT
    )
    assert len(audits) == 1
    assert audits[0].entity_type == AuditEntityType.PROJECT_RESULT
    assert audits[0].entity_id == str(team.project.id)
    assert audits[0].actor_id == team.lead.id


async def test_member_waits_for_due_date(harness: Harness, team: Team) -> None:
    harness.advance(timedelta(days=6))
    assert_problem(await submit(harness, team.a, team), 409, ErrorCode.RESULT_NOT_DUE)
    harness.advance(timedelta(days=1))
    response = await submit(harness, team.a, team)
    assert response.status_code == 201, response.text
    assert response.json()["submitted_by"] == str(team.a.id)
    assert_problem(await submit(harness, team.lead, team), 409, ErrorCode.INVALID_STATE)


async def test_submit_rejections(harness: Harness, team: Team) -> None:
    assert_problem(await submit(harness, team.outsider, team), 403, ErrorCode.FORBIDDEN)
    async with harness.db() as session:
        project = await session.get(models.Project, team.project.id)
        assert project is not None
        project.team_status = "locked"
    assert_problem(await submit(harness, team.lead, team), 409, ErrorCode.INVALID_STATE)
    async with harness.db() as session:
        project = await session.get(models.Project, team.project.id)
        assert project is not None
        project.team_status = "active"
        session.add(
            models.ProjectResult(
                project_id=team.project.id,
                outcome="finished",
                member_areas={},
                participants={},
                auto_closed=False,
            )
        )
    assert_problem(await submit(harness, team.lead, team), 409, ErrorCode.RESULT_EXISTS)


async def test_submit_before_event_end(harness: Harness, team: Team) -> None:
    async with harness.db() as session:
        event = await session.get(models.Event, team.event.id)
        assert event is not None
        event.ends_at = harness.now + timedelta(hours=1)
    assert_problem(await submit(harness, team.lead, team), 409, ErrorCode.INVALID_STATE)
    assert_problem(await submit(harness, team.admin, team), 409, ErrorCode.INVALID_STATE)


async def test_withdrawn_result(harness: Harness, team: Team) -> None:
    response = await submit(harness, team.lead, team, outcome="withdrawn")
    assert response.status_code == 201, response.text
    assert response.json()["allowed_actions"] == []
    async with harness.db() as session:
        project = await session.get(models.Project, team.project.id)
        assert project is not None
        assert project.team_status == "withdrawn"
        assert project.status == "abandoned"
    assert await count(harness, models.ProjectRelease) == 0
    assert await count(harness, models.EventAttendance) == 0
    assert await count(harness, models.Confirmation) == 0
    assert await count(harness, models.ReviewWindow) == 0
    assert await count(harness, models.FeedItem) == 0
    assert len(await harness.pending_jobs("recompute_member")) == 3


async def test_finished_without_links_is_abandoned(harness: Harness, team: Team) -> None:
    response = await submit(
        harness, team.lead, team, repo_url=None, presentation_url="https://slides.test/a"
    )
    assert response.status_code == 201, response.text
    async with harness.db() as session:
        project = await session.get(models.Project, team.project.id)
        assert project is not None
        assert project.team_status == "finished"
        assert project.status == "abandoned"
    assert await count(harness, models.ProjectRelease) == 0
    assert await count(harness, models.FeedItem) == 0
    assert await count(harness, models.EventAttendance) == 2
    assert len(await rows(harness, models.Confirmation)) == 4
    assert await count(harness, models.ReviewWindow) == 1


async def test_team_list_validation(harness: Harness, team: Team) -> None:
    lead, a, b = str(team.lead.id), str(team.a.id), str(team.b.id)
    areas = {lead: "backend", a: "frontend", b: "backend"}
    cases = [
        {"participants": {lead: True, a: True}, "member_areas": {lead: "backend", a: "backend"}},
        {
            "participants": {lead: True, a: True, b: True, str(uuid4()): True},
            "member_areas": areas,
        },
        {"participants": {lead: True, a: True, b: True}, "member_areas": {lead: "backend"}},
        {
            "participants": {lead: True, a: True, b: True},
            "member_areas": {**areas, b: "nonexistent"},
        },
    ]
    for case in cases:
        assert_problem(
            await submit(harness, team.lead, team, **case), 422, ErrorCode.VALIDATION_ERROR
        )
    assert await count(harness, models.ProjectResult) == 0


async def test_award_only_claim_is_unverified(harness: Harness, team: Team) -> None:
    response = await submit(harness, team.lead, team, place=None)
    assert response.status_code == 201, response.text
    assert response.json()["place"] is None
    assert response.json()["place_status"] == "unverified"


async def test_no_claim_has_no_place_status(harness: Harness, team: Team) -> None:
    response = await submit(harness, team.lead, team, place=None, award=None)
    assert response.status_code == 201, response.text
    assert response.json()["place_status"] is None
    releases = await rows(harness, models.ProjectRelease)
    assert releases[0].notes_md == ""


async def test_existing_winner_list_verifies_place(harness: Harness, team: Team) -> None:
    async with harness.db() as session:
        session.add(
            models.EventSourcePerson(
                event_id=team.event.id,
                source_id="devpost:1",
                github_login="alice",
                display_name="Alex",
                role="winner",
                place=2,
                matched_user_id=team.a.id,
            )
        )
    response = await submit(harness, team.lead, team, place=3)
    assert response.status_code == 201, response.text
    async with harness.db() as session:
        result = await session.get(models.ProjectResult, team.project.id)
        assert result is not None
        assert result.place == 2
        assert result.place_status == "verified"
        assert result.place_verified_by is None
        assert result.place_verified_at == harness.now


async def test_winner_without_place_verifies_nothing(harness: Harness, team: Team) -> None:
    async with harness.db() as session:
        session.add(
            models.EventSourcePerson(
                event_id=team.event.id,
                source_id="devpost:1",
                github_login="alice",
                display_name="Alex",
                role="winner",
                place=None,
                matched_user_id=team.a.id,
            )
        )
    response = await submit(harness, team.lead, team, place=3)
    assert response.status_code == 201, response.text
    assert response.json()["place"] == 3
    assert response.json()["place_status"] == "unverified"
    assert response.json()["place_verified_at"] is None


async def test_get_result_visibility(harness: Harness, team: Team) -> None:
    async with await harness.client(team.outsider) as c:
        assert_problem(
            await c.get(f"/api/v1/projects/{team.project.id}/result"), 404, ErrorCode.NOT_FOUND
        )
    assert (await submit(harness, team.lead, team)).status_code == 201
    url = f"/api/v1/projects/{team.project.id}/result"
    async with await harness.client(team.b) as c:
        data = (await c.get(url)).json()
        assert data["retrospective_md"] == "We shipped it."
        assert data["allowed_actions"] == []
    async with await harness.client(team.admin) as c:
        data = (await c.get(url)).json()
        assert data["retrospective_md"] == "We shipped it."
        assert data["allowed_actions"] == ["edit", "verify"]
    async with await harness.client(team.outsider) as c:
        response = await c.get(url)
        assert response.status_code == 200, response.text
        assert response.json()["retrospective_md"] is None
        assert response.json()["allowed_actions"] == []
    async with await harness.client(team.lead) as c:
        data = (await c.get(url)).json()
        assert data["allowed_actions"] == ["edit"]
        assert data["participants"] == {
            str(team.lead.id): True,
            str(team.a.id): True,
            str(team.b.id): False,
        }
