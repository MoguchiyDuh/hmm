import itertools
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from harness import NOW, Harness
from hmm_contracts.common import (
    AreaImportance,
    EnrollmentSource,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    InviteKind,
    InviteStatus,
    JoinRequestStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    RecommendationStatus,
    RecommendationSubjectType,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.recommendations import service
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    areas: tuple[tuple[str, AreaImportance], ...] = (("backend", AreaImportance.PRIMARY),),
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Rec Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=None,
                areas=[
                    catalog.EventAreaInput(key=key, importance=importance)
                    for key, importance in areas
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    *members: models.User,
    team_status: str = "forming",
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=f"Team {next(_titles)}",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=None,
            lead_user_id=lead.id,
            status=ProjectStatus.ACTIVE.value
            if team_status == "locked"
            else ProjectStatus.IDEA.value,
            team_status=team_status,
            origin=ProjectOrigin.CLAIM.value,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if user is lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="claim",
                    is_team_row=True,
                    joined_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def enroll(
    session: AsyncSession,
    event: models.Event,
    user: models.User,
    status: str = "active",
    **fields: Any,
) -> models.Enrollment:
    enrollment = models.Enrollment(
        event_id=event.id,
        user_id=user.id,
        status=status,
        source=EnrollmentSource.SELF.value,
        status_at=NOW,
        **fields,
    )
    session.add(enrollment)
    await session.flush()
    await session.refresh(enrollment)
    return enrollment


async def standalone(
    h: Harness,
    lead: models.User,
    *members: models.User,
    recruiting: dict[str, Any] | None = None,
    event_id: Any = None,
    default_recruiting: bool = True,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event_id,
            name=f"Solo {next(_titles)}",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=recruiting
            if recruiting is not None or not default_recruiting
            else {"areas": ["backend"], "note": "hiring"},
            lead_user_id=lead.id,
            status=ProjectStatus.IDEA.value,
            team_status=None,
            origin=ProjectOrigin.CLAIM.value,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event_id,
                    user_id=user.id,
                    role="lead" if user is lead else "member",
                    state="confirmed",
                    trust="self",
                    joined_via="create",
                    is_team_row=False,
                    joined_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def context_of(h: Harness, project: models.Project) -> service.RecommendationContext:
    async with h.db() as session:
        row = await service.recommendation_project(session, project.id)
        return await service.recommendation_context(session, row, h.now, "fake-1")


async def invite(h: Harness, project: models.Project, user: models.User) -> None:
    async with h.db() as session:
        session.add(
            models.Invite(
                project_id=project.id,
                event_id=project.event_id,
                user_id=user.id,
                invited_by=project.lead_user_id,
                kind=InviteKind.CORE.value if project.event_id else InviteKind.PROJECT.value,
                status=InviteStatus.PENDING.value,
                expires_at=h.now + timedelta(days=3),
            )
        )


async def recommendation_rows(h: Harness) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(models.RecommendationSet)) or 0


def rec(user_id: Any, score: float) -> dict[str, Any]:
    return {
        "user_id": str(user_id),
        "score": score,
        "reasons": ["Strong backend"],
        "tags": ["backend"],
        "suggested_area": "backend",
    }


def test_recommendation_refreshable() -> None:
    def project(team_status: str | None, recruiting: Any = None) -> models.Project:
        return models.Project(team_status=team_status, recruiting=recruiting)

    for status in ("forming", "review", "locked"):
        assert service.recommendation_refreshable(project(status))
    for status in ("active", "finished", "disbanded"):
        assert not service.recommendation_refreshable(project(status))
    assert service.recommendation_refreshable(project(None, {"areas": ["backend"], "note": ""}))
    assert not service.recommendation_refreshable(project(None))


async def test_team_forming_context(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    member = await h.user()
    a = await h.user()
    b = await h.user(dnd_indefinite=True)
    c = await h.user()
    d = await h.user()
    e = await h.user()
    f = await h.user()
    g = await h.user(banned_at=h.now)
    newbie = await h.user(onboarded=False)
    project = await team(h, event, lead, member)
    await team(h, event, d)
    async with h.db() as session:
        await enroll(session, event, lead, "placed", willing_to_lead=True)
        await enroll(
            session,
            event,
            a,
            willing_to_lead=True,
            note="pick me",
            preferred_area_id=areas["backend"].id,
        )
        await enroll(session, event, b)
        await enroll(session, event, c, "reserve", reserve_rank=1)
        await enroll(session, event, d)
        await enroll(session, event, g)
        await enroll(session, event, newbie)
    await invite(h, project, e)
    context = await context_of(h, project)
    request = context.request
    assert [m.user_id for m in request.team] == [lead.id, member.id]
    head = request.team[0]
    assert head.role == "lead"
    assert head.claimed_lead is True
    assert head.state == "confirmed"
    assert head.willing_to_lead is True
    assert head.enrolled_at is not None
    assert request.team[1].claimed_lead is False
    assert request.team[1].enrolled_at == h.now
    assert [m.user_id for m in request.enrolled] == [a.id]
    assert request.enrolled[0].willing_to_lead is True
    assert request.enrolled[0].note == "pick me"
    assert request.enrolled[0].preferred_area == "backend"
    assert [m.user_id for m in request.others] == [c.id, f.id]
    assert context.enrolled_ids == {a.id}
    assert context.others_ids == {c.id, f.id}
    assert request.event is not None
    assert request.event.id == event.id
    assert request.project.areas == []
    assert request.limit_enrolled == request.limit_others == 100
    assert re.fullmatch(r"[0-9a-f]{64}", context.input_hash)
    async with h.db() as session:
        session.add(models.ProjectArea(project_id=project.id, area_id=areas["backend"].id))
    assert (await context_of(h, project)).request.project.areas == ["backend"]


async def test_team_review_and_locked_context(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    for team_status in ("review", "locked"):
        event = await make_hackathon(h)
        lead = await h.user()
        x = await h.user()
        a = await h.user()
        project = await team(h, event, lead, team_status=team_status)
        async with h.db() as session:
            await enroll(session, event, x, "reserve", reserve_rank=1)
            await enroll(session, event, a)
        request = (await context_of(h, project)).request
        assert [m.user_id for m in request.enrolled] == [x.id]
        assert a.id in {m.user_id for m in request.others}
        assert x.id not in {m.user_id for m in request.others}
        assert request.team[0].user_id == lead.id


async def test_standalone_context(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead = await h.user()
    member = await h.user()
    j1 = await h.user()
    j2 = await h.user(dnd_indefinite=True)
    invitee = await h.user()
    plain = await h.user()
    project = await standalone(h, lead, member)
    async with h.db() as session:
        first = models.JoinRequest(
            project_id=project.id,
            user_id=j1.id,
            note="let me in",
            status=JoinRequestStatus.PENDING.value,
        )
        second = models.JoinRequest(
            project_id=project.id, user_id=j2.id, status=JoinRequestStatus.PENDING.value
        )
        session.add_all([first, second])
        await session.flush()
        await session.refresh(first)
    await invite(h, project, invitee)
    request = (await context_of(h, project)).request
    assert request.event is None
    assert request.project.recruiting is not None
    assert request.project.recruiting.areas == ["backend"]
    assert request.team == []
    assert [m.user_id for m in request.enrolled] == [j1.id]
    assert request.enrolled[0].note == "let me in"
    assert request.enrolled[0].enrolled_at == first.created_at
    assert request.enrolled[0].preferred_area is None
    assert request.enrolled[0].willing_to_lead is False
    others = {m.user_id for m in request.others}
    assert plain.id in others
    assert not others & {lead.id, member.id, j1.id, j2.id, invitee.id}

    event = await make_hackathon(h)
    team_lead = await h.user()
    await team(h, event, team_lead)
    hosted = await standalone(h, lead, event_id=event.id)
    request = (await context_of(h, hosted)).request
    assert request.event is not None
    assert request.event.id == event.id
    others = {m.user_id for m in request.others}
    assert team_lead.id not in others
    assert plain.id in others


async def test_read_without_rows(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead = await h.user()
    admin = await h.admin()
    project = await standalone(h, lead)
    jobs_before = await h.pending_jobs()
    async with await h.client(lead) as c:
        response = await c.get(f"/api/v1/projects/{project.id}/recommendations")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "pending",
        "stale": True,
        "computed_at": None,
        "enrolled": [],
        "others": [],
        "allowed_actions": ["refresh"],
    }
    async with await h.client(admin) as c:
        response = await c.get(f"/api/v1/projects/{project.id}/recommendations")
    assert response.status_code == 200, response.text
    assert response.json()["allowed_actions"] == []
    assert await recommendation_rows(h) == 0
    assert await h.pending_jobs() == jobs_before


async def test_read_served_result(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    lead = await h.user()
    first = await h.user(display_name="First Pick")
    second = await h.user(display_name="Second Pick")
    project = await standalone(h, lead)
    context = await context_of(h, project)
    served_at = h.now - timedelta(minutes=5)
    async with h.db() as session:
        session.add(
            models.RecommendationSet(
                subject_type=RecommendationSubjectType.PROJECT.value,
                subject_id=project.id,
                input_hash=context.input_hash,
                status=RecommendationStatus.READY.value,
                enrolled=[rec(second.id, 0.9), rec(first.id, 0.4)],
                others=[],
                algo_version="fake-1",
                created_at=served_at,
                completed_at=served_at,
            )
        )
    url = f"/api/v1/projects/{project.id}/recommendations"

    async def read() -> dict[str, Any]:
        async with await h.client(lead) as c:
            response = await c.get(url)
        assert response.status_code == 200, response.text
        return response.json()

    body = await read()
    assert body["status"] == "ready"
    assert body["stale"] is False
    assert body["computed_at"] == served_at.isoformat().replace("+00:00", "Z")
    assert [item["user_id"] for item in body["enrolled"]] == [str(second.id), str(first.id)]
    assert [item["profile"]["display_name"] for item in body["enrolled"]] == [
        "Second Pick",
        "First Pick",
    ]
    assert body["enrolled"][0]["profile"]["id"] == str(second.id)
    assert body["enrolled"][0]["score"] == 0.9
    assert body["enrolled"][0]["suggested_area"] == "backend"
    assert body["others"] == []

    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=first.id,
                area_id=areas["backend"].id,
                calculated=Decimal("1"),
                calc_reason="x",
                override=Decimal("8"),
                override_evidence="other",
                override_reason="x",
                override_at=h.now,
            )
        )
    assert (await read())["stale"] is True
    async with h.db() as session:
        await session.execute(delete(models.AreaScore).where(models.AreaScore.user_id == first.id))
    assert (await read())["stale"] is False

    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(recruiting={"areas": ["backend"], "note": "changed"})
        )
    assert (await read())["stale"] is True
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(recruiting={"areas": ["backend"], "note": "hiring"})
        )
    assert (await read())["stale"] is False

    async with h.db() as session:
        session.add(
            models.RecommendationSet(
                subject_type=RecommendationSubjectType.PROJECT.value,
                subject_id=project.id,
                input_hash="0" * 64,
                status=RecommendationStatus.FAILED.value,
                error="boom",
                algo_version="fake-1",
                created_at=h.now,
            )
        )
    body = await read()
    assert body["status"] == "failed"
    assert body["stale"] is False
    assert body["computed_at"] == served_at.isoformat().replace("+00:00", "Z")
    assert [item["user_id"] for item in body["enrolled"]] == [str(second.id), str(first.id)]


async def test_read_drops_missing_users(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead = await h.user()
    kept = await h.user()
    project = await standalone(h, lead)
    ghost = uuid4()
    async with h.db() as session:
        session.add(
            models.RecommendationSet(
                subject_type=RecommendationSubjectType.PROJECT.value,
                subject_id=project.id,
                input_hash="f" * 64,
                status=RecommendationStatus.READY.value,
                enrolled=[rec(kept.id, 0.2), rec(ghost, 0.9), rec(kept.id, 0.1)],
                others=[rec(ghost, 0.5), rec(kept.id, 0.3)],
                algo_version="fake-1",
                created_at=h.now,
                completed_at=h.now,
            )
        )
    async with await h.client(lead) as c:
        response = await c.get(f"/api/v1/projects/{project.id}/recommendations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stale"] is True
    assert [(item["user_id"], item["score"]) for item in body["enrolled"]] == [
        (str(kept.id), 0.2),
        (str(kept.id), 0.1),
    ]
    assert [(item["user_id"], item["score"]) for item in body["others"]] == [(str(kept.id), 0.3)]


async def test_read_errors(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    lead = await h.user()
    plain = await h.user()
    admin = await h.admin()
    newbie = await h.user(onboarded=False)
    project = await standalone(h, lead)
    closed = await standalone(h, lead, default_recruiting=False)
    event = await make_hackathon(h)
    active = await team(h, event, await h.user(), team_status="active")
    url = f"/api/v1/projects/{project.id}/recommendations"
    async with await h.client(plain) as c:
        assert_problem(await c.get(url), 403, ErrorCode.FORBIDDEN)
        assert_problem(
            await c.get(f"/api/v1/projects/{uuid4()}/recommendations"), 404, ErrorCode.NOT_FOUND
        )
    async with await h.client(admin) as c:
        assert (await c.get(url)).status_code == 200
        assert_problem(
            await c.get(f"/api/v1/projects/{active.id}/recommendations"),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with await h.client(lead) as c:
        assert_problem(
            await c.get(f"/api/v1/projects/{closed.id}/recommendations"),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.anonymous() as c:
        assert_problem(await c.get(url), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(newbie) as c:
        assert_problem(await c.get(url), 403, ErrorCode.ONBOARDING_REQUIRED)
