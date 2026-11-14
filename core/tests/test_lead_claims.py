import asyncio
import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    EnrollmentSource,
    EnrollmentStatus,
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
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    kind: EventKind = EventKind.HACKATHON,
    status: EventStatus = EventStatus.PUBLISHED,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=kind,
                title=f"Claim Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                areas=[catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY)],
            ),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=None,
            now=h.now,
        )


async def seed_fit(
    h: Harness, event: models.Event, user: models.User, fit: str, reason: str = "fit"
) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(fit),
                reason=reason,
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


async def seed_project(h: Harness, lead: models.User, status: str = "idea") -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=None,
            name="P",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            recruiting=None,
            lead_user_id=lead.id,
            status=status,
            team_status=None,
            origin=ProjectOrigin.STANDALONE.value,
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def seed_team(h: Harness, event: models.Event, lead: models.User) -> models.Project:
    async with h.db() as session:
        team = models.Project(
            event_id=event.id,
            name="Other",
            description_md="",
            goal="",
            links=[],
            visibility=ProjectVisibility.PUBLIC.value,
            lead_user_id=lead.id,
            status=ProjectStatus.IDEA.value,
            team_status=TeamStatus.FORMING.value,
            origin=ProjectOrigin.ADMIN.value,
        )
        session.add(team)
        await session.flush()
        session.add(
            models.ProjectMember(
                project_id=team.id,
                event_id=event.id,
                user_id=lead.id,
                role=MemberRole.LEAD.value,
                state=MemberState.CONFIRMED.value,
                trust=Trust.SELF.value,
                joined_via=JoinedVia.ADMIN.value,
                is_team_row=True,
                joined_at=h.now,
            )
        )
        await session.flush()
        await session.refresh(team)
        return team


async def seed_enrollment(
    h: Harness, event: models.Event, user: models.User, **fields: Any
) -> models.Enrollment:
    async with h.db() as session:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=EnrollmentStatus.ACTIVE.value,
            source=EnrollmentSource.SELF.value,
            status_at=h.now,
            **fields,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def set_phase(h: Harness, event: models.Event, phase: FormationPhase) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value)
        )


async def claim(
    client: httpx.AsyncClient, event: models.Event, name: str = "Core"
) -> httpx.Response:
    return await client.post(f"/api/v1/events/{event.id}/projects", json={"name": name})


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def claimed_projects(h: Harness, event: models.Event) -> int:
    return await count(
        h,
        models.Project,
        models.Project.event_id == event.id,
        models.Project.origin == ProjectOrigin.CLAIM.value,
    )


async def test_claim_happy_path(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    other_lead = await h.user()
    enrollment = await seed_enrollment(
        h,
        event,
        member,
        preferred_area_id=areas["backend"].id,
        note="hi",
        willing_to_lead=True,
    )
    other = await seed_team(h, event, other_lead)
    async with h.db() as session:
        invite = models.Invite(
            project_id=other.id,
            event_id=event.id,
            user_id=member.id,
            invited_by=other_lead.id,
            kind=InviteKind.CORE.value,
            status=InviteStatus.PENDING.value,
            expires_at=h.now + timedelta(days=3),
        )
        request = models.JoinRequest(
            project_id=other.id,
            event_id=event.id,
            user_id=member.id,
            status=JoinRequestStatus.PENDING.value,
        )
        session.add_all([invite, request])
        await session.flush()
        invite_id, request_id = invite.id, request.id
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        response = await claim(c, event)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Core"
    assert body["team_status"] == "forming"
    assert body["status"] == "idea"
    assert body["origin"] == "claim"
    assert body["lead_user_id"] == str(member.id)
    assert body["ready_at"] is None
    assert body["open_seats"] is None
    assert body["members"][0]["role"] == "lead"
    assert body["members"][0]["joined_via"] == "claim"
    assert body["members"][0]["state"] == "confirmed"
    assert {"ready", "unclaim", "add_member"} <= set(body["allowed_actions"])
    async with h.db() as session:
        projects = list(
            await session.scalars(
                select(models.Project).where(models.Project.origin == ProjectOrigin.CLAIM.value)
            )
        )
        assert len(projects) == 1
        project = projects[0]
        members = list(
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
        )
        assert len(members) == 1
        assert members[0].is_team_row
        assert members[0].confirmed_at == h.now
        row = await session.get_one(models.Enrollment, enrollment.id)
        assert row.status == EnrollmentStatus.PLACED.value
        assert row.status_at == h.now
        assert row.reserve_rank is None
        assert (row.preferred_area_id, row.note, row.willing_to_lead) == (
            areas["backend"].id,
            "hi",
            True,
        )
        closed_invite = await session.get_one(models.Invite, invite_id)
        assert closed_invite.status == InviteStatus.CLOSED.value
        assert closed_invite.closed_reason == "claimed_lead"
        assert closed_invite.responded_at == h.now
        closed_request = await session.get_one(models.JoinRequest, request_id)
        assert closed_request.status == JoinRequestStatus.CLOSED.value
        assert closed_request.responded_at == h.now
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "lead_assign")
            )
        )
        assert len(audits) == 1
        assert audits[0].actor_id == member.id
        assert audits[0].actor_kind == "user"
        assert audits[0].entity_type == "project"
        assert audits[0].entity_id == str(project.id)
    refreshes = await h.pending_jobs("refresh_recommendations")
    assert {job["args"]["project_id"] for job in refreshes} == {str(project.id), str(other.id)}
    assert await h.pending_jobs("compute_lead_fits_for_member") == []


async def test_claim_without_enrollment(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        response = await claim(c, event)
    assert response.status_code == 201, response.text
    async with h.db() as session:
        rows = list(
            await session.scalars(
                select(models.Enrollment).where(
                    models.Enrollment.event_id == event.id, models.Enrollment.user_id == member.id
                )
            )
        )
    assert [(row.status, row.source) for row in rows] == [("placed", "self")]


async def test_claim_missing_fit(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_score(h, member, areas["backend"])
    enrollment = await seed_enrollment(h, event, member)
    async with await h.client(member) as c:
        assert_problem(await claim(c, event), 503, ErrorCode.LEAD_FIT_PENDING)
        jobs = await h.pending_jobs("compute_lead_fits_for_member")
        assert [job["args"]["user_id"] for job in jobs] == [str(member.id)]
        assert await claimed_projects(h, event) == 0
        async with h.db() as session:
            row = await session.get_one(models.Enrollment, enrollment.id)
            assert (row.status, row.status_at, row.updated_at) == (
                enrollment.status,
                enrollment.status_at,
                enrollment.updated_at,
            )
        await h.run_jobs()
        response = await claim(c, event)
    assert response.status_code == 201, response.text


async def test_claim_low_fit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_fit(h, event, member, "0.59", "needs experience")
    async with await h.client(member) as c:
        problem = assert_problem(await claim(c, event), 403, ErrorCode.LEAD_FIT_TOO_LOW)
    assert problem.detail == "needs experience"
    assert await claimed_projects(h, event) == 0
    assert await h.pending_jobs() == []


async def test_claim_dnd(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    away = await h.user(dnd_indefinite=True)
    back = await h.user(dnd_until=h.now - timedelta(days=1))
    await seed_fit(h, event, away, "0.9")
    await seed_fit(h, event, back, "0.9")
    async with await h.client(away) as c:
        assert_problem(await claim(c, event), 409, ErrorCode.USER_DND)
    async with await h.client(back) as c:
        assert (await claim(c, event)).status_code == 201


async def test_claim_already_on_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_team(h, event, member)
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        assert_problem(await claim(c, event), 409, ErrorCode.ALREADY_IN_TEAM)
    assert await claimed_projects(h, event) == 0


async def test_claim_phase_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    cases = [
        (FormationPhase.FORMING, ErrorCode.FORMATION_IN_PROGRESS),
        (FormationPhase.REVIEW, ErrorCode.INVALID_STATE),
        (FormationPhase.LOCKED, ErrorCode.INVALID_STATE),
        (FormationPhase.CLOSED, ErrorCode.INVALID_STATE),
    ]
    async with await h.client(member) as c:
        for phase, code in cases:
            event = await make_hackathon(h)
            await set_phase(h, event, phase)
            await seed_fit(h, event, member, "0.9")
            assert_problem(await claim(c, event), 409, code)
        late = await make_hackathon(h, registration_deadline=h.now + timedelta(days=1))
        await seed_fit(h, late, member, "0.9")
        async with h.db() as session:
            await session.execute(
                update(models.Event)
                .where(models.Event.id == late.id)
                .values(registration_deadline=h.now - timedelta(minutes=1))
            )
        assert_problem(await claim(c, late), 409, ErrorCode.ENROLLMENT_CLOSED)


async def test_claim_visibility(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    admin = await h.admin()
    newbie = await h.user(onboarded=False)
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    draft = await make_hackathon(h, status=EventStatus.DRAFT)
    cancelled = await make_hackathon(h)
    await seed_enrollment(h, cancelled, member)
    async with h.db() as session:
        await session.execute(
            update(models.Event)
            .where(models.Event.id == cancelled.id)
            .values(status=EventStatus.CANCELLED.value)
        )
    async with await h.client(member) as c:
        assert_problem(await claim(c, conference), 404, ErrorCode.NOT_FOUND)
        assert_problem(await claim(c, draft), 404, ErrorCode.NOT_FOUND)
        assert_problem(await claim(c, cancelled), 409, ErrorCode.INVALID_STATE)
        response = await c.post(f"/api/v1/events/{uuid4()}/projects", json={"name": "Core"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(admin) as c:
        assert_problem(await claim(c, draft), 409, ErrorCode.INVALID_STATE)
    async with h.anonymous() as c:
        assert_problem(await claim(c, conference), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(newbie) as c:
        assert_problem(await claim(c, cancelled), 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_claim_projects_led_limit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    led = [await seed_project(h, member) for _ in range(5)]
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        assert_problem(await claim(c, event), 409, ErrorCode.PROJECTS_LED_LIMIT)
        async with h.db() as session:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == led[0].id)
                .values(status=ProjectStatus.ABANDONED.value)
            )
        assert (await claim(c, event)).status_code == 201


async def test_claim_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        assert (await claim(c, event)).status_code == 201
        assert_problem(await claim(c, event), 409, ErrorCode.ALREADY_IN_TEAM)
    assert await claimed_projects(h, event) == 1
    assert await count(h, models.ProjectMember, models.ProjectMember.event_id == event.id) == 1
    assert await count(h, models.AuditLog, models.AuditLog.action == "lead_assign") == 1


async def test_claim_concurrent(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    await seed_fit(h, event, member, "0.9")
    async with await h.client(member) as c:
        responses = await asyncio.gather(claim(c, event), claim(c, event))
    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert_problem(conflict, 409, ErrorCode.ALREADY_IN_TEAM)
    assert await claimed_projects(h, event) == 1
