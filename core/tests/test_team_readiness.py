import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service
from hmm_core.platform.settings import load_settings
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Ready Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=team_size_min,
                team_size_max=team_size_max,
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def seed_fit(h: Harness, event: models.Event, user: models.User, fit: str = "0.9") -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(fit),
                reason="fit",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def seed_score(h: Harness, user: models.User, area: models.Area, value: int = 10) -> None:
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=user.id, area_id=area.id, calculated=Decimal(value), calc_reason="x"
            )
        )


async def claim(client: httpx.AsyncClient, event: models.Event, name: str = "Core") -> UUID:
    response = await client.post(f"/api/v1/events/{event.id}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def enroll(
    h: Harness, event: models.Event, user: models.User, status: str = "active"
) -> models.Enrollment:
    async with h.db() as session:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=False,
            status_at=h.now,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def add_member(h: Harness, project_id: UUID, event: models.Event, user: models.User) -> None:
    async with h.db() as session:
        session.add(
            models.ProjectMember(
                project_id=project_id,
                event_id=event.id,
                user_id=user.id,
                role=MemberRole.MEMBER.value,
                state=MemberState.CONFIRMED.value,
                trust=Trust.SELF.value,
                joined_via=JoinedVia.INVITE.value,
                is_team_row=True,
                joined_at=h.now,
                confirmed_at=h.now,
            )
        )


async def set_phase(h: Harness, event: models.Event, phase: FormationPhase) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value)
        )


async def set_team_status(h: Harness, project_id: UUID, team_status: TeamStatus) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project_id)
            .values(team_status=team_status.value)
        )


async def lead_team(h: Harness, event: models.Event) -> tuple[models.User, UUID]:
    lead = await h.user()
    await seed_fit(h, event, lead)
    async with await h.client(lead) as c:
        project_id = await claim(c, event)
    return lead, project_id


async def formation_of(h: Harness, event: models.Event) -> models.EventFormation:
    async with h.db() as session:
        return await session.get_one(models.EventFormation, event.id)


async def project_of(h: Harness, project_id: UUID) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


async def notifications(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def mark_ready(h: Harness, user: models.User, project_id: UUID) -> httpx.Response:
    async with await h.client(user) as c:
        return await c.post(f"/api/v1/projects/{project_id}/ready")


async def clear_ready(h: Harness, user: models.User, project_id: UUID) -> httpx.Response:
    async with await h.client(user) as c:
        return await c.delete(f"/api/v1/projects/{project_id}/ready")


async def test_formation_sizes(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    default = await make_hackathon(h)
    custom = await make_hackathon(h, team_size_min=3, team_size_max=6)
    async with h.db() as session:
        settings = await load_settings(session)
        for event, expected in ((default, (2, 5, 5, 4, 3)), (custom, (3, 6, 6, 5, 3))):
            formation = await session.get_one(models.EventFormation, event.id)
            sizes = service.formation_sizes(event, formation, settings)
            assert (
                sizes.min_size,
                sizes.max_size,
                sizes.target,
                sizes.viable,
                sizes.core_max,
            ) == expected


async def test_unclaim(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    first = await h.user()
    second = await h.user()
    outsider = await h.user()
    asker = await h.user()
    for user in (first, second):
        await enroll(h, event, user, status="placed")
        await add_member(h, project_id, event, user)
    async with h.db() as session:
        invite = models.Invite(
            project_id=project_id,
            event_id=event.id,
            user_id=outsider.id,
            invited_by=lead.id,
            kind=InviteKind.CORE.value,
            status=InviteStatus.PENDING.value,
            expires_at=h.now + timedelta(days=3),
        )
        request = models.JoinRequest(
            project_id=project_id,
            event_id=event.id,
            user_id=asker.id,
            status=JoinRequestStatus.PENDING.value,
        )
        session.add_all([invite, request])
        await session.flush()
        invite_id, request_id = invite.id, request.id
    async with await h.client(lead) as c:
        response = await c.post(f"/api/v1/projects/{project_id}/unclaim")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["team_status"] == "disbanded"
    assert body["status"] == "disbanded"
    assert body["lead_user_id"] is None
    assert body["members"] == []
    async with h.db() as session:
        members = list(
            await session.scalars(
                select(models.ProjectMember).where(models.ProjectMember.project_id == project_id)
            )
        )
        assert len(members) == 3
        assert all(member.left_at == h.now for member in members)
        assert all(member.left_reason == "disbanded" for member in members)
        enrollments = list(
            await session.scalars(
                select(models.Enrollment).where(models.Enrollment.event_id == event.id)
            )
        )
        assert len(enrollments) == 3
        assert all(row.status == "active" and row.status_at == h.now for row in enrollments)
        closed_invite = await session.get_one(models.Invite, invite_id)
        assert closed_invite.status == "closed"
        assert closed_invite.closed_reason == "team_disbanded"
        assert closed_invite.responded_at == h.now
        closed_request = await session.get_one(models.JoinRequest, request_id)
        assert closed_request.status == "closed"
        assert closed_request.responded_at == h.now
        project = await session.get_one(models.Project, project_id)
        assert project.ready_at is None
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "project_disband")
            )
        )
        assert [audit.actor_kind for audit in audits] == ["user"]
    sent = await notifications(h, "team_disbanded")
    assert sorted(row.user_id for row in sent) == sorted([first.id, second.id])
    assert all(
        isinstance(row.payload, dict) and row.payload["reason"] == "The lead unclaimed the team"
        for row in sent
    )
    jobs = await h.pending_jobs("refresh_recommendations")
    assert [job["args"]["project_id"] for job in jobs] == [str(project_id)]
    async with await h.client(lead) as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{project_id}/unclaim"), 409, ErrorCode.INVALID_STATE
        )
    assert len(await notifications(h, "team_disbanded")) == 2
    assert await count(h, models.AuditLog, models.AuditLog.action == "project_disband") == 1


async def test_unclaim_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    member = await h.user()
    await add_member(h, project_id, event, member)
    async with await h.client(member) as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{project_id}/unclaim"), 403, ErrorCode.FORBIDDEN
        )
    async with await h.client(lead) as c:
        await set_phase(h, event, FormationPhase.FORMING)
        assert_problem(
            await c.post(f"/api/v1/projects/{project_id}/unclaim"),
            409,
            ErrorCode.FORMATION_IN_PROGRESS,
        )
        await set_phase(h, event, FormationPhase.OPEN)
        await set_team_status(h, project_id, TeamStatus.REVIEW)
        assert_problem(
            await c.post(f"/api/v1/projects/{project_id}/unclaim"), 409, ErrorCode.INVALID_STATE
        )
        assert_problem(
            await c.post(f"/api/v1/projects/{uuid4()}/unclaim"), 404, ErrorCode.NOT_FOUND
        )
        async with h.db() as session:
            standalone = models.Project(
                event_id=None,
                name="P",
                description_md="",
                goal="",
                links=[],
                visibility=ProjectVisibility.PUBLIC.value,
                lead_user_id=lead.id,
                status=ProjectStatus.IDEA.value,
                team_status=None,
                origin=ProjectOrigin.STANDALONE.value,
            )
            session.add(standalone)
            await session.flush()
            standalone_id = standalone.id
        assert_problem(
            await c.post(f"/api/v1/projects/{standalone_id}/unclaim"), 404, ErrorCode.NOT_FOUND
        )


async def test_ready_toggle(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    response = await mark_ready(h, lead, project_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert datetime.fromisoformat(body["ready_at"]) == h.now
    assert datetime.fromisoformat(body["last_lead_action_at"]) == h.now
    before = await project_of(h, project_id)
    response = await mark_ready(h, lead, project_id)
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["ready_at"]) == h.now
    assert (await project_of(h, project_id)).updated_at == before.updated_at
    response = await clear_ready(h, lead, project_id)
    assert response.status_code == 204
    assert response.content == b""
    cleared = await project_of(h, project_id)
    assert cleared.ready_at is None
    assert (await clear_ready(h, lead, project_id)).status_code == 204
    assert (await project_of(h, project_id)).updated_at == cleared.updated_at


async def test_ready_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    member = await h.user()
    await add_member(h, project_id, event, member)
    assert_problem(await mark_ready(h, member, project_id), 403, ErrorCode.FORBIDDEN)
    await set_phase(h, event, FormationPhase.FORMING)
    assert_problem(await mark_ready(h, lead, project_id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_phase(h, event, FormationPhase.OPEN)
    await set_team_status(h, project_id, TeamStatus.REVIEW)
    assert_problem(await mark_ready(h, lead, project_id), 409, ErrorCode.INVALID_STATE)
    assert_problem(await mark_ready(h, lead, uuid4()), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{project_id}/ready"), 401, ErrorCode.UNAUTHENTICATED
        )


async def test_early_start(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    others = [await h.user() for _ in range(3)]
    for user in others:
        await enroll(h, event, user)
    assert (await mark_ready(h, lead, project_id)).status_code == 200
    formation = await formation_of(h, event)
    assert formation.formation_at == h.now + timedelta(hours=1)
    assert formation.review_ends_at == h.now + timedelta(hours=49)
    sent = await notifications(h, "formation_rescheduled")
    assert sorted(row.user_id for row in sent) == sorted([lead.id, *(user.id for user in others)])
    for row in sent:
        assert isinstance(row.payload, dict)
        assert datetime.fromisoformat(
            str(row.payload["previous_formation_at"])
        ) == h.now + timedelta(days=25)
        assert datetime.fromisoformat(str(row.payload["formation_at"])) == h.now + timedelta(
            hours=1
        )
        assert row.payload["reason"]
    assert (await mark_ready(h, lead, project_id)).status_code == 200
    assert len(await notifications(h, "formation_rescheduled")) == 4


async def test_early_start_waits_for_every_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    first_lead, first = await lead_team(h, event)
    second_lead, second = await lead_team(h, event)
    for _ in range(2):
        await enroll(h, event, await h.user())
    original = (await formation_of(h, event)).formation_at
    assert (await mark_ready(h, first_lead, first)).status_code == 200
    assert (await formation_of(h, event)).formation_at == original
    assert await notifications(h, "formation_rescheduled") == []
    assert (await mark_ready(h, second_lead, second)).status_code == 200
    moved = (await formation_of(h, event)).formation_at
    assert moved == h.now + timedelta(hours=1)
    assert len(await notifications(h, "formation_rescheduled")) == 4
    assert (await clear_ready(h, first_lead, first)).status_code == 204
    assert (await formation_of(h, event)).formation_at == moved


async def test_early_start_needs_viable_enrollments(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    for _ in range(2):
        await enroll(h, event, await h.user())
    await enroll(h, event, await h.user(), status="reserve")
    original = (await formation_of(h, event)).formation_at
    assert (await mark_ready(h, lead, project_id)).status_code == 200
    assert (await formation_of(h, event)).formation_at == original
    assert await notifications(h, "formation_rescheduled") == []
    await enroll(h, event, await h.user())
    assert (await clear_ready(h, lead, project_id)).status_code == 204
    assert (await mark_ready(h, lead, project_id)).status_code == 200
    assert (await formation_of(h, event)).formation_at == h.now + timedelta(hours=1)
    assert len(await notifications(h, "formation_rescheduled")) == 4


async def test_early_start_never_moves_back(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead, project_id = await lead_team(h, event)
    for _ in range(3):
        await enroll(h, event, await h.user())
    soon = h.now + timedelta(minutes=30)
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(formation_at=soon)
        )
    assert (await mark_ready(h, lead, project_id)).status_code == 200
    assert (await formation_of(h, event)).formation_at == soon
    assert await notifications(h, "formation_rescheduled") == []


async def test_early_start_requires_claimed_leads(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    lead = await h.user()
    for _ in range(4):
        await enroll(h, event, await h.user())
    async with h.db() as session:
        session.add(
            models.Project(
                event_id=event.id,
                name="Formed",
                description_md="",
                goal="",
                links=[],
                visibility=ProjectVisibility.PUBLIC.value,
                lead_user_id=lead.id,
                status=ProjectStatus.IDEA.value,
                team_status=TeamStatus.FORMING.value,
                origin=ProjectOrigin.FORMATION.value,
                ready_at=h.now,
            )
        )
    original = (await formation_of(h, event)).formation_at
    async with h.db() as session:
        locked = await session.get_one(models.Event, event.id)
        formation = await session.get_one(models.EventFormation, event.id)
        assert await service.maybe_early_start(session, locked, formation, h.now) is False
    assert (await formation_of(h, event)).formation_at == original
