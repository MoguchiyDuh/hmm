import itertools
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from harness import Harness
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
    LeftReason,
    MemberState,
    ProjectStatus,
    TeamStatus,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from sqlalchemy import func, select, update
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Placement Hack {next(_titles)}",
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


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "placed",
    willing: bool = False,
) -> models.Enrollment:
    async with h.db() as session:
        existing = await formation.live_enrollment_row(session, event.id, user.id)
        if existing is not None:
            return existing
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=willing,
            status_at=h.now,
            reserve_rank=None,
        )
        session.add(enrollment)
        await session.flush()
        await session.refresh(enrollment)
        return enrollment


async def seed_project(
    h: Harness,
    event: models.Event,
    *,
    name: str = "Team",
    team_status: str = "review",
    origin: str = "claim",
    review_ends_at: datetime | None = None,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=None,
            status="idea",
            team_status=team_status,
            origin=origin,
            ready_at=None,
            review_ends_at=review_ends_at,
            last_lead_action_at=None,
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def add_member(
    h: Harness,
    event: models.Event,
    project: models.Project,
    user: models.User,
    *,
    role: str = "member",
    state: str = "confirmed",
    joined_via: str = "formation",
    confirm_by: datetime | None = None,
    confirm_kind: str | None = None,
    area_key: str | None = None,
    set_lead: bool = False,
) -> models.ProjectMember:
    async with h.db() as session:
        area_id = (
            None
            if area_key is None
            else await session.scalar(select(models.Area.id).where(models.Area.key == area_key))
        )
        member = models.ProjectMember(
            project_id=project.id,
            event_id=event.id,
            user_id=user.id,
            role=role,
            area_id=area_id,
            state=state,
            trust="self",
            joined_via=joined_via,
            proposal_reason=None,
            confirm_by=confirm_by,
            confirm_kind=confirm_kind,
            is_team_row=True,
            joined_at=h.now,
            confirmed_at=h.now if state == "confirmed" else None,
        )
        session.add(member)
        if set_lead:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == project.id)
                .values(lead_user_id=user.id)
            )
        await session.flush()
        await session.refresh(member)
        return member


async def member_of(
    h: Harness, event: models.Event, project: models.Project, *, lead: bool = False, **fields: Any
) -> tuple[models.User, models.ProjectMember]:
    user = await h.user()
    await enroll(h, event, user)
    row = await add_member(
        h,
        event,
        project,
        user,
        role="lead" if lead else "member",
        set_lead=lead,
        **fields,
    )
    return user, row


async def proposed_of(
    h: Harness,
    event: models.Event,
    project: models.Project,
    *,
    kind: str = "placement",
    lead: bool = False,
    confirm_by: datetime | None = None,
) -> tuple[models.User, models.ProjectMember]:
    return await member_of(
        h,
        event,
        project,
        lead=lead,
        state="proposed",
        confirm_kind=kind,
        confirm_by=confirm_by or h.now + timedelta(days=1),
    )


async def stored_member(h: Harness, member_id: Any) -> models.ProjectMember:
    async with h.db() as session:
        return await session.get_one(models.ProjectMember, member_id)


async def stored_project(h: Harness, project_id: Any) -> models.Project:
    async with h.db() as session:
        return await session.get_one(models.Project, project_id)


async def enrollment_of(h: Harness, event: models.Event, user: models.User) -> models.Enrollment:
    async with h.db() as session:
        row = await session.scalar(
            select(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id)
            .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
            .limit(1)
        )
        assert row is not None
        return row


async def notified(h: Harness, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.type == kind)
            )
        )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def audits(h: Harness, project: models.Project) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.entity_id == str(project.id))
            )
        )


async def todo_jobs(h: Harness) -> list[str]:
    return sorted(job["task_name"] for job in await h.pending_jobs() if job["status"] == "todo")


async def set_phase(h: Harness, event: models.Event, phase: FormationPhase) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value)
        )


async def set_team_status(h: Harness, project: models.Project, team_status: TeamStatus) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(team_status=team_status.value)
        )


async def confirm(h: Harness, user: models.User, project_id: Any) -> Any:
    async with await h.client(user) as c:
        return await c.post(f"/api/v1/projects/{project_id}/placement/confirm")


async def decline(h: Harness, user: models.User, project_id: Any, withdraw: bool = False) -> Any:
    async with await h.client(user) as c:
        return await c.post(
            f"/api/v1/projects/{project_id}/placement/decline", json={"withdraw": withdraw}
        )


async def test_confirm_placement(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    await member_of(h, event, team, lead=True)
    await member_of(h, event, team)
    user, row = await proposed_of(h, event, team)
    notifications = await count(h, models.Notification)
    jobs = await todo_jobs(h)

    response = await confirm(h, user, team.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "confirmed"
    assert datetime.fromisoformat(body["confirmed_at"]) == h.now
    assert body["confirm_by"] is None
    assert body["confirm_kind"] is None
    assert body["allowed_actions"] == []
    stored = await stored_member(h, row.id)
    assert (stored.state, stored.confirmed_at, stored.confirm_by, stored.confirm_kind) == (
        "confirmed",
        h.now,
        None,
        None,
    )
    assert await count(h, models.Notification) == notifications
    assert await todo_jobs(h) == jobs
    assert await audits(h, team) == []


async def test_confirm_reconfirm(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    lead, lead_row = await proposed_of(h, event, team, kind="reconfirm", lead=True)
    core, core_row = await proposed_of(h, event, team, kind="reconfirm")
    _, other_row = await proposed_of(h, event, team)
    _, confirmed_row = await member_of(h, event, team)

    for user, row in ((lead, lead_row), (core, core_row)):
        response = await confirm(h, user, team.id)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "confirmed"
        assert (await stored_member(h, row.id)).state == "confirmed"
    untouched = await stored_member(h, other_row.id)
    assert (untouched.state, untouched.confirm_kind) == ("proposed", "placement")
    assert (await stored_member(h, confirmed_row.id)).state == "confirmed"


async def test_confirm_presence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="locked")
    await member_of(h, event, team, lead=True)
    user, row = await proposed_of(
        h, event, team, kind="presence", confirm_by=event.starts_at - timedelta(hours=12)
    )

    response = await confirm(h, user, team.id)
    assert response.status_code == 200, response.text
    assert (await stored_member(h, row.id)).state == "confirmed"


async def test_confirm_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    user, row = await proposed_of(h, event, team)

    assert (await confirm(h, user, team.id)).status_code == 200
    confirmed_at = (await stored_member(h, row.id)).confirmed_at
    notifications = await count(h, models.Notification)
    jobs = await todo_jobs(h)
    h.advance(timedelta(minutes=5))
    response = await confirm(h, user, team.id)
    assert response.status_code == 200, response.text
    assert (await stored_member(h, row.id)).confirmed_at == confirmed_at
    assert await count(h, models.Notification) == notifications
    assert await todo_jobs(h) == jobs
    assert await count(h, models.ProjectMember, models.ProjectMember.project_id == team.id) == 1


async def test_confirm_wrong_actors(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    other = await seed_project(h, event, name="Other")
    await proposed_of(h, event, team)
    outsider, _ = await proposed_of(h, event, other)
    teammate, teammate_row = await member_of(h, event, team)

    assert_problem(await confirm(h, outsider, team.id), 403, ErrorCode.FORBIDDEN)
    response = await confirm(h, teammate, team.id)
    assert response.status_code == 200, response.text
    assert (await stored_member(h, teammate_row.id)).confirmed_at == teammate_row.confirmed_at
    async with h.anonymous() as c:
        assert_problem(
            await c.post(f"/api/v1/projects/{team.id}/placement/confirm"),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
    assert_problem(await confirm(h, teammate, uuid4()), 404, ErrorCode.NOT_FOUND)


async def test_confirm_wrong_state(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="forming")
    user, _ = await proposed_of(h, event, team)

    assert_problem(await confirm(h, user, team.id), 409, ErrorCode.INVALID_STATE)
    await set_team_status(h, team, TeamStatus.FINISHED)
    assert_problem(await confirm(h, user, team.id), 409, ErrorCode.INVALID_STATE)
    await set_team_status(h, team, TeamStatus.REVIEW)
    await set_phase(h, event, FormationPhase.FORMING)
    assert_problem(await confirm(h, user, team.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    async with h.db() as session:
        standalone = models.Project(
            event_id=None,
            name="Solo",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=user.id,
            status=ProjectStatus.IDEA.value,
            team_status=None,
            origin="standalone",
        )
        session.add(standalone)
        await session.flush()
        standalone_id = standalone.id
    assert_problem(await confirm(h, user, standalone_id), 404, ErrorCode.NOT_FOUND)


async def test_decline_keeps_reserve_seat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    lead, _ = await member_of(h, event, team, lead=True)
    mate, _ = await member_of(h, event, team)
    user, row = await proposed_of(h, event, team)

    response = await decline(h, user, team.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reserve"
    assert body["reserve_rank"] is None
    assert datetime.fromisoformat(body["status_at"]) == h.now
    assert body["team"] is None
    assert body["allowed_actions"] == ["edit", "withdraw"]
    stored = await stored_member(h, row.id)
    assert (stored.left_at, stored.left_reason) == (h.now, LeftReason.DECLINED.value)
    notices = await notified(h, "team_member_left")
    assert sorted(n.user_id for n in notices) == sorted([lead.id, mate.id])
    assert all(cast(dict[str, Any], n.payload)["member_count"] == 2 for n in notices)
    assert await todo_jobs(h) == ["rank_reserve", "refresh_recommendations"]
    assert await audits(h, team) == []


async def test_decline_withdraws(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    await member_of(h, event, team, lead=True)
    user, _ = await proposed_of(h, event, team)
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=user.id,
                status="going",
                role="attendee",
                proof_links=[],
                trust="self",
                status_at=h.now,
            )
        )

    response = await decline(h, user, team.id, withdraw=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "withdrawn"
    assert body["allowed_actions"] == []
    enrollment = await enrollment_of(h, event, user)
    assert (enrollment.status, enrollment.reserve_rank) == ("withdrawn", None)
    async with h.db() as session:
        attendance = await session.scalar(
            select(models.EventAttendance).where(
                models.EventAttendance.event_id == event.id,
                models.EventAttendance.user_id == user.id,
            )
        )
    assert attendance is not None
    assert attendance.status == "cancelled"
    assert await todo_jobs(h) == ["refresh_recommendations"]


async def test_claimed_lead_declines(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    lead, lead_row = await proposed_of(h, event, team, kind="reconfirm", lead=True)
    first, _ = await member_of(h, event, team)
    second, _ = await member_of(h, event, team)

    response = await decline(h, lead, team.id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reserve"
    project = await stored_project(h, team.id)
    assert project.lead_user_id is None
    assert (await stored_member(h, lead_row.id)).left_reason == "declined"
    assert (await enrollment_of(h, event, lead)).status == "reserve"
    async with h.db() as session:
        row = await session.get_one(models.Project, team.id)
        top = (await formation.lead_order(session, row, h.now))[0].user.id
        offers = list(
            await session.scalars(
                select(models.LeadOffer).where(
                    models.LeadOffer.project_id == team.id,
                    models.LeadOffer.status == "pending",
                )
            )
        )
    assert [(o.user_id, o.source, o.reason) for o in offers] == [
        (top, "succession", "The claimed lead declined the placement")
    ]
    assert [str(value) for value in project.lead_succession_excluded] == [str(lead.id)]
    assert [(a.action, a.actor_id) for a in await audits(h, team)] == [("lead_step_down", lead.id)]
    assert sorted(n.user_id for n in await notified(h, "team_member_left")) == sorted(
        [first.id, second.id]
    )


async def test_decline_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    other = await seed_project(h, event, name="Other")
    confirmed, _ = await member_of(h, event, team)
    outsider, _ = await member_of(h, event, other)
    user, _ = await proposed_of(h, event, team)

    assert_problem(await decline(h, confirmed, team.id), 409, ErrorCode.INVALID_STATE)
    assert_problem(await decline(h, outsider, team.id), 403, ErrorCode.FORBIDDEN)
    assert (await decline(h, user, team.id)).status_code == 200
    assert_problem(await decline(h, user, team.id), 403, ErrorCode.FORBIDDEN)

    forming = await seed_project(h, event, name="Forming", team_status="forming")
    pending, _ = await proposed_of(h, event, forming)
    await set_phase(h, event, FormationPhase.OPEN)
    assert_problem(await decline(h, pending, forming.id), 409, ErrorCode.INVALID_STATE)
    await set_team_status(h, forming, TeamStatus.REVIEW)
    await set_phase(h, event, FormationPhase.FORMING)
    assert_problem(await decline(h, pending, forming.id), 409, ErrorCode.FORMATION_IN_PROGRESS)
    await set_phase(h, event, FormationPhase.REVIEW)
    await set_team_status(h, forming, TeamStatus.FINISHED)
    assert_problem(await decline(h, pending, forming.id), 409, ErrorCode.INVALID_STATE)


async def test_decline_presence_in_locked_team(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event, team_status="locked")
    await member_of(h, event, team, lead=True)
    user, _ = await proposed_of(
        h, event, team, kind="presence", confirm_by=event.starts_at - timedelta(hours=12)
    )

    response = await decline(h, user, team.id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reserve"
    assert (await stored_project(h, team.id)).seat_open_since is None
    assert await notified(h, "seat_opened") == []


async def test_lead_action_confirms(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    team = await seed_project(h, event)
    lead, lead_row = await proposed_of(h, event, team, kind="reconfirm", lead=True)
    mate, _ = await member_of(h, event, team)

    async with await h.client(lead) as c:
        response = await c.post(
            f"/api/v1/projects/{team.id}/transfer-lead", json={"user_id": str(mate.id)}
        )
    assert response.status_code == 201, response.text
    stored = await stored_member(h, lead_row.id)
    assert (stored.state, stored.confirm_by) == (MemberState.CONFIRMED.value, None)
    assert (await stored_project(h, team.id)).last_lead_action_at == h.now

    h.advance(timedelta(minutes=5))
    async with h.db() as session:
        project = await session.get_one(models.Project, team.id)
        actor = await session.get_one(models.User, mate.id)
        assert await formation.note_lead_action(session, project, actor, h.now) is None
    assert (await stored_project(h, team.id)).last_lead_action_at == h.now - timedelta(minutes=5)
