import asyncio
import itertools
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness import NOW, Harness
from hmm_contracts.common import (
    AreaImportance,
    EnrollmentSource,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.recommendations import service
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_api_skeleton import assert_problem

_titles = itertools.count(1)

JOB = "refresh_recommendations"


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
                title=f"Rec Refresh Hack {next(_titles)}",
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
            if team_status in {"locked", "active", "finished"}
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
    *,
    recruiting: dict[str, Any] | None = None,
    event_id: UUID | None = None,
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
        session.add(
            models.ProjectMember(
                project_id=project.id,
                event_id=event_id,
                user_id=lead.id,
                role="lead",
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


async def refresh_stamp(h: Harness, project: models.Project) -> datetime | None:
    async with h.db() as session:
        return await session.scalar(
            select(models.Project.recommendation_refresh_at).where(models.Project.id == project.id)
        )


async def recommendation_rows(h: Harness) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(models.RecommendationSet)) or 0


def url(project_id: UUID) -> str:
    return f"/api/v1/projects/{project_id}/recommendations/refresh"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def forming(h: Harness) -> tuple[models.Project, models.Event, models.User]:
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    project = await team(h, event, lead, await h.user())
    return project, event, lead


async def test_lead_refresh_enqueues_job(harness: Harness) -> None:
    h = harness
    project, _, lead = await forming(h)
    async with await h.client(lead) as c:
        response = await c.post(url(project.id))
    assert response.status_code == 202, response.text
    assert response.json() is None
    assert await refresh_stamp(h, project) == h.now
    [job] = await h.pending_jobs(JOB)
    assert job["args"] == {"project_id": str(project.id)}
    assert job["lock"] == f"rec:{project.id}"
    assert job["queueing_lock"] == f"refresh_recommendations:{project.id}"
    assert await recommendation_rows(h) == 0


async def test_refresh_is_throttled(harness: Harness) -> None:
    h = harness
    project, _, lead = await forming(h)
    async with await h.client(lead) as c:
        assert (await c.post(url(project.id))).status_code == 202
        assert_problem(await c.post(url(project.id)), 429, ErrorCode.RATE_LIMITED)
    assert await refresh_stamp(h, project) == h.now
    assert len(await h.pending_jobs(JOB)) == 1
    await h.run_jobs()
    later = h.advance(timedelta(seconds=61))
    async with await h.client(lead) as c:
        response = await c.post(url(project.id))
    assert response.status_code == 202, response.text
    assert await refresh_stamp(h, project) == later
    assert len(await h.pending_jobs(JOB)) == 2


async def test_refresh_permissions_and_errors(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    member = await h.user()
    admin = await h.admin()
    newbie = await h.user(onboarded=False)
    project = await team(h, event, lead, member)
    closed = await standalone(h, lead, default_recruiting=False)
    active_lead = await h.user()
    active = await team(h, event, active_lead, team_status="active")
    async with await h.client(member) as c:
        assert_problem(await c.post(url(project.id)), 403, ErrorCode.FORBIDDEN)
    async with await h.client(admin) as c:
        assert_problem(await c.post(url(project.id)), 403, ErrorCode.FORBIDDEN)
        assert_problem(await c.post(url(uuid4())), 404, ErrorCode.NOT_FOUND)
    async with await h.client(lead) as c:
        assert_problem(await c.post(url(closed.id)), 409, ErrorCode.INVALID_STATE)
    async with await h.client(active_lead) as c:
        assert_problem(await c.post(url(active.id)), 409, ErrorCode.INVALID_STATE)
    async with h.anonymous() as c:
        assert_problem(await c.post(url(project.id)), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(newbie) as c:
        assert_problem(await c.post(url(project.id)), 403, ErrorCode.ONBOARDING_REQUIRED)
    for row in (project, closed, active):
        assert await refresh_stamp(h, row) is None
    assert await h.pending_jobs(JOB) == []


async def test_rollback_emits_no_job(harness: Harness) -> None:
    h = harness
    project, _, lead = await forming(h)
    async with h.sessionmaker() as session:
        await service.request_refresh(session, h.jobs, project.id, lead, h.now)
        await session.rollback()
    assert await refresh_stamp(h, project) is None
    assert await h.pending_jobs(JOB) == []


async def test_concurrent_refresh(harness: Harness) -> None:
    h = harness
    project, _, lead = await forming(h)
    async with await h.client(lead) as c1, await h.client(lead) as c2:
        responses = await asyncio.gather(c1.post(url(project.id)), c2.post(url(project.id)))
    codes = sorted(response.status_code for response in responses)
    assert codes == [202, 429]
    limited = next(response for response in responses if response.status_code == 429)
    assert_problem(limited, 429, ErrorCode.RATE_LIMITED)
    assert len(await h.pending_jobs(JOB)) == 1
    assert await refresh_stamp(h, project) == h.now


async def test_event_edit_refreshes_projects(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    admin = await h.admin()
    open_team = await team(h, event, await h.user())
    await team(h, event, await h.user(), team_status="finished")
    hiring = await standalone(h, await h.user(), event_id=event.id)
    await standalone(h, await h.user(), event_id=event.id, default_recruiting=False)
    base = len(await h.pending_jobs(JOB))
    patch = f"/api/v1/admin/events/{event.id}"
    async with await h.client(admin) as c:
        response = await c.patch(patch, json={"title": "Renamed"})
        assert response.status_code == 200, response.text
        jobs = await h.pending_jobs(JOB)
        assert len(jobs) == base + 2
        assert {job["args"]["project_id"] for job in jobs[base:]} == {
            str(open_team.id),
            str(hiring.id),
        }
        await h.run_jobs()
        response = await c.patch(patch, json={"title": "Renamed"})
        assert response.status_code == 200, response.text
        assert len(await h.pending_jobs(JOB)) == base + 2
        await h.run_jobs()
        response = await c.patch(patch, json={"team_size_max": 6})
        assert response.status_code == 200, response.text
    jobs = await h.pending_jobs(JOB)
    assert {job["args"]["project_id"] for job in jobs[base + 2 :]} == {
        str(open_team.id),
        str(hiring.id),
    }
    assert len(jobs) == base + 4


async def test_enqueue_project_refresh_dedupes(harness: Harness) -> None:
    h = harness
    project, event, _ = await forming(h)
    other = await team(h, event, await h.user())
    async with h.db() as session:
        await service.enqueue_project_refresh(session, h.jobs, project.id)
        await service.enqueue_project_refresh(session, h.jobs, project.id)
        await service.enqueue_project_refresh(session, h.jobs, other.id)
    jobs = await h.pending_jobs(JOB)
    assert [job["args"]["project_id"] for job in jobs] == [str(project.id), str(other.id)]


async def test_refresh_end_to_end(harness: Harness) -> None:
    h = harness
    project, event, lead = await forming(h)
    async with h.db() as session:
        await enroll(session, event, await h.user())
        await enroll(session, event, await h.user())
    read = f"/api/v1/projects/{project.id}/recommendations"
    async with await h.client(lead) as c:
        assert (await c.post(url(project.id))).status_code == 202
        await h.run_jobs()
        body = (await c.get(read)).json()
        assert body["status"] == "ready"
        assert body["stale"] is False
        assert body["computed_at"] == iso(h.now)
        assert len(body["enrolled"]) == 2
        late = await h.user()
        async with h.db() as session:
            await enroll(session, event, late)
        assert (await c.get(read)).json()["stale"] is True
        h.advance(timedelta(minutes=1))
        assert (await c.post(url(project.id))).status_code == 202
        await h.run_jobs()
        body = (await c.get(read)).json()
    assert body["status"] == "ready"
    assert body["stale"] is False
    assert body["computed_at"] == iso(h.now)
    assert str(late.id) in {item["user_id"] for item in body["enrolled"]}
