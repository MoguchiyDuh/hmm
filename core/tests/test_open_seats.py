import itertools
from datetime import datetime, timedelta
from decimal import Decimal
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
    TeamStatus,
)
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import schemas
from hmm_core.formation import service as formation
from hmm_core.notifications.service import raise_alert
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
                title=f"Seat Hack {next(_titles)}",
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


async def set_phase(h: Harness, event: models.Event, phase: str, **values: Any) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase, **values)
        )


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "placed",
    willing: bool = False,
    reserve_rank: int | None = None,
) -> models.Enrollment:
    async with h.db() as session:
        area_id = await session.scalar(select(models.Area.id).where(models.Area.key == "backend"))
        row = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=willing,
            status_at=h.now,
            reserve_rank=reserve_rank,
            preferred_area_id=area_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def seed_project(
    h: Harness,
    event: models.Event,
    *,
    name: str = "Team",
    team_status: str = "locked",
    origin: str = "claim",
    status: str = "active",
    review_ends_at: datetime | None = None,
) -> models.Project:
    async with h.db() as session:
        row = models.Project(
            event_id=event.id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=None,
            status=status,
            team_status=team_status,
            origin=origin,
            ready_at=None,
            review_ends_at=review_ends_at,
            last_lead_action_at=None,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


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
    enrolled_status: str = "placed",
) -> models.ProjectMember:
    async with h.db() as session:
        live = await formation.live_enrollment_row(session, event.id, user.id)
    if live is None:
        await enroll(h, event, user, status=enrolled_status)
    async with h.db() as session:
        area_id = (
            await session.scalar(select(models.Area.id).where(models.Area.key == area_key))
            if area_key is not None
            else None
        )
        row = models.ProjectMember(
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
        session.add(row)
        if set_lead:
            await session.execute(
                update(models.Project)
                .where(models.Project.id == project.id)
                .values(lead_user_id=user.id)
            )
        await session.flush()
        await session.refresh(row)
        return row


async def notifications_for(h: Harness, user: models.User, kind: str) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.user_id == user.id, models.Notification.type == kind
                )
            )
        )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def test_reserve_list_ranked_read_only(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    a, b, c = await h.user(), await h.user(), await h.user()
    banned = await h.user(banned_at=h.now)
    rows = [
        await enroll(h, event, a, status="reserve", reserve_rank=2),
        await enroll(h, event, b, status="reserve", reserve_rank=1),
        await enroll(h, event, c, status="reserve"),
    ]
    await enroll(h, event, await h.user(), status="active")
    await enroll(h, event, await h.user(), status="placed")
    await enroll(h, event, banned, status="reserve", reserve_rank=3)
    async with h.db() as session:
        before = [
            (row.id, row.reserve_rank, row.updated_at)
            for row in await session.scalars(select(models.Enrollment))
        ]
    audits = await count(h, models.AuditLog)
    async with await h.client(b) as client:
        response = await client.get(f"/api/v1/events/{event.id}/reserve")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [row["user"]["id"] for row in data] == [str(b.id), str(a.id), str(c.id)]
    assert [row["reserve_rank"] for row in data] == [1, 2, None]
    assert all(row["status"] == "reserve" and row["team"] is None for row in data)
    assert [row["allowed_actions"] for row in data] == [["edit", "withdraw"], [], []]
    async with h.db() as session:
        after = [
            (row.id, row.reserve_rank, row.updated_at)
            for row in await session.scalars(select(models.Enrollment))
        ]
    assert after == before
    assert await count(h, models.AuditLog) == audits
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert [row.reserve_rank for row in rows] == [2, 1, None]


async def test_reserve_list_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member, unfinished = await h.user(), await h.user(onboarded=False)
    path = f"/api/v1/events/{event.id}/reserve"
    async with h.anonymous() as client:
        assert_problem(await client.get(path), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(unfinished) as client:
        assert_problem(await client.get(path), 403, ErrorCode.ONBOARDING_REQUIRED)
    async with await h.client(member) as client:
        assert_problem(
            await client.get(f"/api/v1/events/{uuid4()}/reserve"), 404, ErrorCode.NOT_FOUND
        )
        async with h.db() as session:
            await session.execute(
                update(models.Event).where(models.Event.id == event.id).values(status="draft")
            )
        assert_problem(await client.get(path), 404, ErrorCode.NOT_FOUND)
        async with h.db() as session:
            await session.execute(
                update(models.Event)
                .where(models.Event.id == event.id)
                .values(status="published", formation_enabled=False)
            )
        assert_problem(await client.get(path), 404, ErrorCode.NOT_FOUND)


async def test_reserve_reorder_audit_and_jobs(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, FormationPhase.LOCKED)
    admin = await h.admin()
    users = [await h.user() for _ in range(3)]
    for index, user in enumerate(users, start=1):
        await enroll(h, event, user, status="reserve", reserve_rank=index)
    teams = [
        await seed_project(h, event, name="Locked", team_status=TeamStatus.LOCKED),
        await seed_project(h, event, name="Review", team_status=TeamStatus.REVIEW),
    ]
    await seed_project(h, event, name="Disbanded", team_status=TeamStatus.DISBANDED)
    ordered = [users[2], users[0], users[1]]
    ids = [str(user.id) for user in ordered]
    async with await h.client(admin) as client:
        response = await client.put(
            f"/api/v1/admin/events/{event.id}/reserve/order",
            json={"user_ids": ids, "reason": "Manual order"},
        )
    assert response.status_code == 200, response.text
    assert [row["user"]["id"] for row in response.json()] == ids
    assert [row["reserve_rank"] for row in response.json()] == [1, 2, 3]
    async with h.db() as session:
        ranks = {
            user_id: rank
            for user_id, rank in await session.execute(
                select(models.Enrollment.user_id, models.Enrollment.reserve_rank).where(
                    models.Enrollment.event_id == event.id
                )
            )
        }
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == "reserve_reorder")
            )
        )
    assert ranks == {user.id: index for index, user in enumerate(ordered, start=1)}
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_id == admin.id
    assert audit.actor_kind == "admin"
    assert audit.entity_type == "formation"
    assert audit.entity_id == str(event.id)
    assert audit.before == {"user_ids": [str(user.id) for user in users]}
    assert audit.after == {"user_ids": ids}
    assert audit.reason == "Manual order"
    assert await count(h, models.Notification) == 0
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert len(pending) == len(teams)
    assert all(job["task_name"] == "refresh_recommendations" for job in pending)
    assert {job["args"]["project_id"] for job in pending} == {str(team.id) for team in teams}


async def test_reserve_reorder_validation_atomic(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, FormationPhase.REVIEW)
    admin = await h.admin()
    a, b = await h.user(), await h.user()
    await enroll(h, event, a, status="reserve", reserve_rank=1)
    await enroll(h, event, b, status="reserve", reserve_rank=2)
    audits = await count(h, models.AuditLog)
    async with await h.client(admin) as client:
        for ids in ([str(a.id)], [str(a.id), str(uuid4())], [str(a.id), str(a.id)], []):
            response = await client.put(
                f"/api/v1/admin/events/{event.id}/reserve/order",
                json={"user_ids": ids, "reason": "Invalid order"},
            )
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
            async with h.db() as session:
                ranks = list(
                    await session.scalars(
                        select(models.Enrollment.reserve_rank).order_by(
                            models.Enrollment.reserve_rank
                        )
                    )
                )
            assert ranks == [1, 2]
            assert await count(h, models.AuditLog) == audits
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_reserve_reorder_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member, admin = await h.user(), await h.admin()
    await enroll(h, event, member, status="reserve", reserve_rank=1)
    path = f"/api/v1/admin/events/{event.id}/reserve/order"
    body = {"user_ids": [str(member.id)], "reason": "Manual order"}
    async with await h.client(member) as client:
        assert_problem(await client.put(path, json=body), 403, ErrorCode.FORBIDDEN)
    async with await h.client(admin) as client:
        assert_problem(await client.put(path, json=body), 409, ErrorCode.INVALID_STATE)
        await set_phase(h, event, FormationPhase.FORMING)
        assert_problem(await client.put(path, json=body), 409, ErrorCode.FORMATION_IN_PROGRESS)
        assert_problem(
            await client.put(f"/api/v1/admin/events/{uuid4()}/reserve/order", json=body),
            404,
            ErrorCode.NOT_FOUND,
        )


async def test_reserve_reorder_actions(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    admin, member = await h.admin(), await h.user()
    for phase in (FormationPhase.OPEN, FormationPhase.REVIEW, FormationPhase.LOCKED):
        await set_phase(h, event, phase)
        async with h.db() as session:
            row = await formation.formation_row(session, event.id)
            settings = await load_settings(session)
            for actor in (admin, member):
                actions = formation.formation_actions(
                    event,
                    row,
                    actor=actor,
                    enrollment=None,
                    on_team=False,
                    lead_fit=None,
                    now=h.now,
                    reg_deadline=event.starts_at,
                    settings=settings,
                )
                assert (schemas.FormationAction.REORDER_RESERVE in actions) == (
                    actor.is_admin and phase in (FormationPhase.REVIEW, FormationPhase.LOCKED)
                )


async def open_project_seat(
    h: Harness, event: models.Event, project: models.Project
) -> models.Project:
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        current_event = await session.get_one(models.Event, event.id)
        await formation.open_seat(session, row, current_event, h.now)
        await session.refresh(row)
        return row


async def seed_below_min_alert(h: Harness, project: models.Project) -> None:
    async with h.db() as session:
        await raise_alert(
            session,
            AlertKind.TEAM_BELOW_MIN,
            h.now,
            title="Team below minimum size",
            message="Missing members",
            link=f"/admin/projects/{project.id}",
            subject_type="project",
            subject_id=project.id,
            subject_title=project.name,
            dedupe_key=f"team_below_min:{project.id}",
        )


async def test_open_seat_notice_and_dedupe(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    lead, member, reserve = await h.user(), await h.user(), await h.user()
    project = await seed_project(h, event)
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await add_member(h, event, project, member)
    await enroll(h, event, reserve, status="reserve", reserve_rank=1)
    audits = await count(h, models.AuditLog)
    row = await open_project_seat(h, event, project)
    assert row.seat_open_since == h.now
    [notification] = await notifications_for(h, lead, "seat_opened")
    payload = cast(dict[str, Any], notification.payload)
    assert payload["open_seats"] == 3
    assert datetime.fromisoformat(payload["head_start_ends_at"]) == h.now + timedelta(hours=12)
    assert datetime.fromisoformat(payload["reg_deadline"]) == h.now + timedelta(days=30)
    assert payload["quick_pick"][0]["user_id"] == str(reserve.id)
    assert payload["quick_pick"][0]["rank"] == 1
    assert notification.dedupe_key == f"seat_opened:{project.id}:{h.now.isoformat()}"
    h.advance(timedelta(hours=1))
    again = await open_project_seat(h, event, project)
    assert again.seat_open_since == row.seat_open_since
    assert len(await notifications_for(h, lead, "seat_opened")) == 1
    assert await count(h, models.Notification) == 1
    assert await count(h, models.AuditLog) == audits
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_open_seat_full_team_clears_timer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    for _ in range(5):
        await add_member(h, event, project, await h.user())
    assert (await open_project_seat(h, event, project)).seat_open_since is None
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(seat_open_since=h.now - timedelta(hours=2))
        )
        member = await session.scalar(
            select(models.ProjectMember).where(models.ProjectMember.project_id == project.id)
        )
        assert member is not None
        member.left_at = h.now
    await add_member(h, event, project, await h.user())
    assert (await open_project_seat(h, event, project)).seat_open_since is None
    assert await count(h, models.Notification) == 0


async def test_open_seat_ignores_nonlocked_teams(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, "locked")
    for team_status in ("review", "forming", "active", "finished"):
        project = await seed_project(h, event, team_status=team_status)
        before = project.updated_at
        row = await open_project_seat(h, event, project)
        assert row.seat_open_since is None
        assert row.updated_at == before
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AdminAlert) == 0


async def test_open_seat_closed_above_min_reopens_below_min(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead = await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await add_member(h, event, project, await h.user())
    await enroll(h, event, await h.user(), status="reserve")
    closed_at = h.now - timedelta(hours=1)
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(seats_closed_at=closed_at)
        )
    row = await open_project_seat(h, event, project)
    assert row.seats_closed_at == closed_at
    assert row.seat_open_since == h.now
    assert not await notifications_for(h, lead, "seat_opened")
    async with h.db() as session:
        await session.execute(
            update(models.Event).where(models.Event.id == event.id).values(team_size_min=3)
        )
    row = await open_project_seat(h, event, project)
    assert row.seats_closed_at is None
    assert len(await notifications_for(h, lead, "seat_opened")) == 1


async def test_open_seat_syncs_below_min_alert(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    for _ in range(2):
        await add_member(h, event, project, await h.user())
    await seed_below_min_alert(h, project)
    await open_project_seat(h, event, project)
    async with h.db() as session:
        alert = await session.scalar(
            select(models.AdminAlert).where(models.AdminAlert.subject_id == project.id)
        )
        assert alert is not None
        assert alert.status == "resolved"
        assert alert.resolution_note is not None
    small_event = await make_hackathon(h, team_size_min=3, team_size_max=5)
    await set_phase(h, small_event, "locked")
    small = await seed_project(h, small_event)
    await add_member(h, small_event, small, await h.user())
    await open_project_seat(h, small_event, small)
    async with h.db() as session:
        alert = await session.scalar(
            select(models.AdminAlert).where(models.AdminAlert.subject_id == small.id)
        )
        assert alert is not None
        assert alert.status == "open"
        assert alert.severity == "warning"
        assert alert.dedupe_key == f"team_below_min:{small.id}"
        assert alert.subject_id == small.id
    another_event = await make_hackathon(h, team_size_min=3, team_size_max=5)
    await set_phase(h, another_event, "locked")
    another = await seed_project(h, another_event)
    await add_member(h, another_event, another, await h.user())
    await enroll(h, another_event, await h.user(), status="reserve")
    await open_project_seat(h, another_event, another)
    assert await count(h, models.AdminAlert, models.AdminAlert.subject_id == another.id) == 0


async def test_close_seats_confirms_lead_and_is_idempotent(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead = await h.user()
    lead_row = await add_member(
        h,
        event,
        project,
        lead,
        role="lead",
        state="proposed",
        confirm_kind="reconfirm",
        confirm_by=h.now + timedelta(hours=2),
        set_lead=True,
    )
    for _ in range(2):
        await add_member(h, event, project, await h.user())
    since = h.now - timedelta(hours=1)
    async with h.db() as session:
        await session.execute(
            update(models.Project)
            .where(models.Project.id == project.id)
            .values(seat_open_since=since)
        )
    audits = await count(h, models.AuditLog)
    async with await h.client(lead) as client:
        response = await client.post(f"/api/v1/projects/{project.id}/seats/close")
        assert response.status_code == 200, response.text
        data = response.json()
        assert datetime.fromisoformat(data["seats_closed_at"]) == h.now
        assert datetime.fromisoformat(data["seat_open_since"]) == since
        assert data["open_seats"] == 2
        assert "reopen_seats" in data["allowed_actions"]
        h.advance(timedelta(minutes=1))
        again = await client.post(f"/api/v1/projects/{project.id}/seats/close")
        assert again.status_code == 200, again.text
        assert again.json()["seats_closed_at"] == data["seats_closed_at"]
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        member = await session.get_one(models.ProjectMember, lead_row.id)
        assert row.last_lead_action_at == h.now
        assert member.state == "confirmed"
        assert member.confirmed_at == h.now - timedelta(minutes=1)
        assert member.confirm_kind is None
        assert member.confirm_by is None
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == audits
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_close_seats_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead, member = await h.user(), await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    path = f"/api/v1/projects/{project.id}/seats/close"
    async with h.anonymous() as client:
        assert_problem(await client.post(path), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(member) as client:
        assert_problem(await client.post(path), 403, ErrorCode.FORBIDDEN)
    async with await h.client(lead) as client:
        assert_problem(await client.post(path), 409, ErrorCode.SEATS_CLOSE_BELOW_MIN)
        assert_problem(
            await client.post(f"/api/v1/projects/{uuid4()}/seats/close"), 404, ErrorCode.NOT_FOUND
        )
        async with h.db() as session:
            row = await session.get_one(models.Project, project.id)
            row.team_status = "review"
        assert_problem(await client.post(path), 409, ErrorCode.INVALID_STATE)
        await set_phase(h, event, "forming")
        assert_problem(await client.post(path), 409, ErrorCode.FORMATION_IN_PROGRESS)
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.seats_closed_at is None
        assert row.last_lead_action_at is None


async def test_reopen_seats_notice_and_idempotence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead = await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await add_member(h, event, project, await h.user())
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        row.seats_closed_at = h.now - timedelta(hours=1)
    audits = await count(h, models.AuditLog)
    opened_at = h.now
    async with await h.client(lead) as client:
        response = await client.delete(f"/api/v1/projects/{project.id}/seats/close")
        assert response.status_code == 204, response.text
        assert not response.content
        h.advance(timedelta(minutes=1))
        response = await client.delete(f"/api/v1/projects/{project.id}/seats/close")
        assert response.status_code == 204, response.text
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.seats_closed_at is None
        assert row.seat_open_since == opened_at
        assert row.last_lead_action_at == h.now
    assert len(await notifications_for(h, lead, "seat_opened")) == 1
    assert await count(h, models.AuditLog) == audits
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_reopen_seats_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead, member = await h.user(), await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    path = f"/api/v1/projects/{project.id}/seats/close"
    async with await h.client(member) as client:
        assert_problem(await client.delete(path), 403, ErrorCode.FORBIDDEN)
    async with await h.client(lead) as client:
        for team_status in ("review", "active"):
            async with h.db() as session:
                row = await session.get_one(models.Project, project.id)
                row.team_status = team_status
            assert_problem(await client.delete(path), 409, ErrorCode.INVALID_STATE)


async def test_fill_invite_acceptance_resolves_below_min(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead, invitee = await h.user(), await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await enroll(h, event, invitee, status="reserve")
    await seed_below_min_alert(h, project)
    async with h.db() as session:
        invite = models.Invite(
            project_id=project.id,
            event_id=event.id,
            user_id=invitee.id,
            invited_by=lead.id,
            kind="fill",
            status="pending",
            expires_at=h.now + timedelta(days=1),
        )
        session.add(invite)
        await session.flush()
        await session.refresh(invite)
    async with await h.client(invitee) as client:
        response = await client.post(f"/api/v1/invites/{invite.id}/accept")
    assert response.status_code == 200, response.text
    async with h.db() as session:
        alert = await session.scalar(
            select(models.AdminAlert).where(models.AdminAlert.subject_id == project.id)
        )
        assert alert is not None
        assert alert.status == "resolved"
        assert alert.resolution_note == "The team reached the minimum size"


async def test_seat_candidates_recommendations_then_filtered_reserve(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    a, b, c, unranked = [await h.user() for _ in range(4)]
    banned = await h.user(banned_at=h.now)
    dnd = await h.user(dnd_indefinite=True)
    pending, departed = await h.user(), await h.user()
    for rank, user in enumerate((a, b, c, banned, dnd, pending, departed), start=1):
        await enroll(h, event, user, status="reserve", reserve_rank=rank)
    await enroll(h, event, unranked, status="reserve")
    other_project = await seed_project(h, event, name="Other team")
    await add_member(
        h,
        event,
        other_project,
        pending,
        state="proposed",
        joined_via="reserve_offer",
        confirm_kind="placement",
        confirm_by=h.now + timedelta(hours=1),
    )
    departed_row = await add_member(h, event, project, departed)
    async with h.db() as session:
        row = await session.get_one(models.ProjectMember, departed_row.id)
        row.left_at = h.now
        row.left_reason = "left"
        session.add(
            models.RecommendationSet(
                subject_type="project",
                subject_id=project.id,
                input_hash="seat-candidates",
                status="ready",
                enrolled=[
                    {
                        "user_id": str(user.id),
                        "score": 0.8,
                        "reasons": [reason],
                        "tags": [],
                        "suggested_area": "backend",
                    }
                    for user, reason in (
                        (banned, "Banned"),
                        (dnd, "DND"),
                        (pending, "Pending offer"),
                        (departed, "Departed"),
                        (c, "Best recommendation"),
                        (c, "Duplicate recommendation"),
                        (b, "Second recommendation"),
                    )
                ],
                others=[],
                algo_version="fake-1",
                completed_at=h.now,
            )
        )
    async with h.db() as session:
        candidates = await formation.reserve_candidates(session, project, event, h.now)
        assert [candidate.user.id for candidate in candidates] == [c.id, b.id, a.id, unranked.id]
        assert [candidate.rank for candidate in candidates] == [1, 2, 3, 4]
        assert [candidate.reason for candidate in candidates] == [
            "Best recommendation",
            "Second recommendation",
            "Recommended from the reserve",
            "Recommended from the reserve",
        ]
        assert all(candidate.area_key == "backend" for candidate in candidates)
        quick_pick = await formation.seat_quick_pick(session, project, event, h.now)
        assert [candidate.user_id for candidate in quick_pick] == [c.id, b.id, a.id]
        assert len(await formation.seat_quick_pick(session, project, event, h.now, limit=1)) == 1
        standalone = models.Project(event_id=None)
        assert await formation.reserve_candidates(session, standalone, event, h.now) == []
    assert await count(h, models.Notification) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_open_seat_deadline_and_cancelled_event(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    deadline = h.now + timedelta(hours=1)
    event = await make_hackathon(
        h, team_size_min=2, team_size_max=5, registration_deadline=deadline
    )
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead = await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await enroll(h, event, await h.user(), status="reserve")
    await open_project_seat(h, event, project)
    [notification] = await notifications_for(h, lead, "seat_opened")
    payload = cast(dict[str, Any], notification.payload)
    assert datetime.fromisoformat(payload["head_start_ends_at"]) == deadline
    assert await count(h, models.AdminAlert) == 0
    h.advance(timedelta(hours=1))
    await open_project_seat(h, event, project)
    assert len(await notifications_for(h, lead, "seat_opened")) == 1
    assert await count(h, models.AdminAlert, models.AdminAlert.status == "open") == 1
    cancelled = await make_hackathon(h)
    cancelled_project = await seed_project(h, cancelled)
    await add_member(h, cancelled, cancelled_project, await h.user(), role="lead", set_lead=True)
    async with h.db() as session:
        row = await session.get_one(models.Event, cancelled.id)
        row.status = "cancelled"
    row = await open_project_seat(h, cancelled, cancelled_project)
    assert row.seat_open_since is None
    assert await count(h, models.Notification) == 1


async def test_reserve_add_review_and_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "review")
    review_ends_at = h.now + timedelta(days=2)
    project = await seed_project(
        h, event, team_status="review", status="idea", review_ends_at=review_ends_at
    )
    lead, reserve = await h.user(), await h.user()
    lead_member = await add_member(
        h,
        event,
        project,
        lead,
        role="lead",
        set_lead=True,
        state="proposed",
        confirm_kind="reconfirm",
        confirm_by=h.now + timedelta(hours=1),
    )
    for _ in range(2):
        await add_member(h, event, project, await h.user())
    enrollment = await enroll(h, event, reserve, status="reserve", reserve_rank=1)
    path = f"/api/v1/projects/{project.id}/members"
    body = {"user_id": str(reserve.id), "area_key": "backend"}
    async with await h.client(lead) as client:
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["state"] == "proposed"
        assert data["joined_via"] == "reserve_add"
        assert data["confirm_kind"] == "placement"
        assert data["area_key"] == "backend"
        assert datetime.fromisoformat(data["confirm_by"]) == review_ends_at
        assert data["confirmed_at"] is None
        assert data["allowed_actions"] == ["edit"]
        async with h.db() as session:
            member = await session.scalar(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.user_id == reserve.id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
            assert member is not None
            assert member.proposal_reason == "Added from the reserve by the lead"
            assert str(member.id) == data["id"]
            assert member.joined_at == h.now
            row = await session.get_one(models.Enrollment, enrollment.id)
            assert (row.status, row.reserve_rank, row.status_at) == ("placed", None, h.now)
            team = await session.get_one(models.Project, project.id)
            assert team.last_lead_action_at == h.now
            lead_row = await session.get_one(models.ProjectMember, lead_member.id)
            assert lead_row.state == "confirmed"
            assert lead_row.confirmed_at == h.now
            assert lead_row.confirm_kind is None
            assert lead_row.confirm_by is None
        [notification] = await notifications_for(h, reserve, "team_proposed")
        payload = cast(dict[str, Any], notification.payload)
        assert len(payload["members"]) == 4
        assert payload["members"][0]["user_id"] == str(lead.id)
        assert datetime.fromisoformat(payload["confirm_by"]) == review_ends_at
        assert payload["area_key"] == "backend"
        assert payload["project_member_id"] == data["id"]
        pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
        assert sorted(job["task_name"] for job in pending_before) == [
            "rank_reserve",
            "refresh_recommendations",
        ]
        assert await count(h, models.AuditLog) == 0
        repeat = await client.post(path, json=body)
        assert_problem(repeat, 409, ErrorCode.INVALID_STATE)
    assert (
        await count(
            h,
            models.ProjectMember,
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == reserve.id,
            models.ProjectMember.left_at.is_(None),
        )
        == 1
    )
    assert len(await notifications_for(h, reserve, "team_proposed")) == 1
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before


async def test_reserve_add_locked_updates_seats_and_alerts(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    for size in (2, 4):
        project = await seed_project(h, event, name=f"Team of {size}")
        lead, reserve = await h.user(), await h.user()
        await add_member(h, event, project, lead, role="lead", set_lead=True)
        for _ in range(size - 1):
            await add_member(h, event, project, await h.user())
        await enroll(h, event, reserve, status="reserve", reserve_rank=1)
        since = h.now - timedelta(hours=1)
        async with h.db() as session:
            row = await session.get_one(models.Project, project.id)
            row.seat_open_since = since
        await seed_below_min_alert(h, project)
        async with await h.client(lead) as client:
            response = await client.post(
                f"/api/v1/projects/{project.id}/members",
                json={"user_id": str(reserve.id), "area_key": "backend"},
            )
        assert response.status_code == 201, response.text
        async with h.db() as session:
            row = await session.get_one(models.Project, project.id)
            assert row.seat_open_since == (None if size == 4 else since)
            alert = await session.scalar(
                select(models.AdminAlert).where(models.AdminAlert.subject_id == project.id)
            )
            assert alert is not None
            assert alert.status == "resolved"


async def test_reserve_add_deadline_formula(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    reg_deadline = h.now + timedelta(hours=16)
    event = await make_hackathon(
        h, team_size_min=2, team_size_max=5, registration_deadline=reg_deadline
    )
    await set_phase(h, event, "review")
    for hours in (3, 20, -1):
        review_ends_at = h.now + timedelta(hours=hours)
        project = await seed_project(
            h, event, team_status="review", status="idea", review_ends_at=review_ends_at
        )
        lead, reserve = await h.user(), await h.user()
        await add_member(h, event, project, lead, role="lead", set_lead=True)
        await enroll(h, event, reserve, status="reserve")
        async with await h.client(lead) as client:
            response = await client.post(
                f"/api/v1/projects/{project.id}/members",
                json={"user_id": str(reserve.id)},
            )
        assert response.status_code == 201, response.text
        expected = min(max(review_ends_at, h.now + timedelta(hours=12)), reg_deadline)
        assert datetime.fromisoformat(response.json()["confirm_by"]) == expected


async def test_reserve_add_departed_rule_is_team_specific(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "review")
    lead, departed = await h.user(), await h.user()
    project = await seed_project(h, event, team_status="review", status="idea")
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    old_member = await add_member(h, event, project, departed)
    async with h.db() as session:
        member = await session.get_one(models.ProjectMember, old_member.id)
        member.left_at = h.now - timedelta(hours=1)
        member.left_reason = "declined"
        row = await formation.live_enrollment_row(session, event.id, departed.id)
        assert row is not None
        row.status = "reserve"
        row.reserve_rank = 1
    other = await seed_project(h, event, name="Other", team_status="review", status="idea")
    other_lead = await h.user()
    await add_member(h, event, other, other_lead, role="lead", set_lead=True)
    async with await h.client(lead) as client:
        body = {"user_id": str(departed.id)}
        response = await client.post(f"/api/v1/projects/{project.id}/members", json=body)
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
        assert "already left this team" in response.json()["detail"]
    async with await h.client(other_lead) as client:
        response = await client.post(f"/api/v1/projects/{other.id}/members", json=body)
        assert response.status_code == 201, response.text


async def test_reserve_add_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "review")
    project = await seed_project(h, event, team_status="review", status="idea")
    lead, outsider, reserve = await h.user(), await h.user(), await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await enroll(h, event, reserve, status="reserve", reserve_rank=1)
    path = f"/api/v1/projects/{project.id}/members"
    body = {"user_id": str(reserve.id)}
    async with await h.client(outsider) as client:
        assert_problem(await client.post(path, json=body), 403, ErrorCode.FORBIDDEN)
    async with await h.client(lead) as client:
        for team_status in ("forming", "active"):
            async with h.db() as session:
                row = await session.get_one(models.Project, project.id)
                row.team_status = team_status
            assert_problem(await client.post(path, json=body), 409, ErrorCode.INVALID_STATE)
        async with h.db() as session:
            row = await session.get_one(models.Project, project.id)
            row.team_status = "review"
        assert_problem(
            await client.post(path, json={"user_id": str(uuid4())}), 404, ErrorCode.NOT_FOUND
        )
        assert_problem(
            await client.post(f"/api/v1/projects/{uuid4()}/members", json=body),
            404,
            ErrorCode.NOT_FOUND,
        )
        active = await h.user()
        await enroll(h, event, active, status="active")
        assert_problem(
            await client.post(path, json={"user_id": str(active.id)}),
            409,
            ErrorCode.NOT_ENROLLED,
        )
        banned = await h.user(banned_at=h.now)
        await enroll(h, event, banned, status="reserve")
        assert_problem(
            await client.post(path, json={"user_id": str(banned.id)}),
            409,
            ErrorCode.INVALID_STATE,
        )
        other = await seed_project(h, event, name="Other")
        already_placed = await h.user()
        await add_member(h, event, other, already_placed)
        assert_problem(
            await client.post(path, json={"user_id": str(already_placed.id)}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await client.post(path, json={**body, "area_key": "unknown-area"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        await set_phase(h, event, "forming")
        assert_problem(await client.post(path, json=body), 409, ErrorCode.FORMATION_IN_PROGRESS)
        await set_phase(h, event, "review")
        for _ in range(4):
            await add_member(h, event, project, await h.user())
        assert_problem(await client.post(path, json=body), 409, ErrorCode.TEAM_FULL)
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.last_lead_action_at is None
        enrollment = await formation.live_enrollment_row(session, event.id, reserve.id)
        assert enrollment is not None
        assert (enrollment.status, enrollment.reserve_rank) == ("reserve", 1)
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_reserve_add_locked_registration_deadline(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h,
        team_size_min=2,
        team_size_max=5,
        registration_deadline=h.now + timedelta(hours=1),
    )
    await set_phase(h, event, "locked")
    project = await seed_project(h, event)
    lead, reserve = await h.user(), await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    await enroll(h, event, reserve, status="reserve")
    h.advance(timedelta(hours=2))
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/members", json={"user_id": str(reserve.id)}
        )
    assert_problem(response, 409, ErrorCode.ENROLLMENT_CLOSED)


async def seed_autofill_team(
    h: Harness,
    event: models.Event,
    *,
    size: int = 2,
    name: str = "Autofill team",
    seat_open_since: datetime | None = None,
) -> tuple[models.Project, models.User]:
    project = await seed_project(h, event, name=name)
    lead = await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    for _ in range(size - 1):
        await add_member(h, event, project, await h.user())
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        row.seat_open_since = seat_open_since
    return project, lead


async def run_autofill(h: Harness, event: models.Event) -> int:
    async with h.db() as session:
        row = await session.scalar(
            select(models.Event).where(models.Event.id == event.id).with_for_update()
        )
        assert row is not None
        return await formation.autofill_seats(session, h.jobs, row, h.now)


async def test_autofill_offers_audits_jobs_and_repeat(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project, lead = await seed_autofill_team(h, event, seat_open_since=h.now - timedelta(hours=13))
    users = [await h.user() for _ in range(3)]
    enrollments = [
        await enroll(h, event, user, status="reserve", reserve_rank=rank)
        for rank, user in enumerate(users, start=1)
    ]
    assert await run_autofill(h, event) == 3
    async with h.db() as session:
        members = list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.joined_via == "reserve_offer",
                )
                .order_by(models.ProjectMember.id)
            )
        )
        assert [member.user_id for member in members] == [user.id for user in users]
        for member in members:
            assert member.state == "proposed"
            assert member.confirm_kind == "placement"
            assert member.confirm_by == h.now + timedelta(hours=12)
            assert member.confirmed_at is None
            assert member.area_id is None
            assert member.proposal_reason == "Offered an open seat from the reserve"
        for enrollment in enrollments:
            row = await session.get_one(models.Enrollment, enrollment.id)
            assert (row.status, row.reserve_rank, row.status_at) == ("placed", None, h.now)
        team = await session.get_one(models.Project, project.id)
        assert team.seat_open_since is None
        unchanged = team.updated_at
        audits = list(await session.scalars(select(models.AuditLog)))
    assert len(audits) == 3
    assert {audit.entity_id for audit in audits} == {str(member.id) for member in members}
    assert all(
        audit.action == "system_transition"
        and audit.actor_kind == "system"
        and audit.actor_id is None
        and audit.entity_type == "project_member"
        and audit.before is None
        and audit.reason == "Offered an open seat from the reserve"
        for audit in audits
    )
    for rank, user in enumerate(users, start=1):
        [notification] = await notifications_for(h, user, "reserve_offer")
        payload = cast(dict[str, Any], notification.payload)
        assert payload["rank"] == rank
        assert datetime.fromisoformat(payload["confirm_by"]) == h.now + timedelta(hours=12)
        assert payload["proposal_reason"] == "Offered an open seat from the reserve"
        assert payload["members"][0]["user_id"] == str(lead.id)
        assert len(payload["members"]) == rank + 2
        assert notification.dedupe_key == f"reserve_offer:{members[rank - 1].id}"
    assert not await notifications_for(h, lead, "seat_autofilled")
    pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert sorted(job["task_name"] for job in pending_before) == [
        "rank_reserve",
        "refresh_recommendations",
    ]
    assert await run_autofill(h, event) == 0
    assert await count(h, models.ProjectMember, models.ProjectMember.project_id == project.id) == 5
    assert await count(h, models.Notification) == 3
    assert await count(h, models.AuditLog) == 3
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before
    async with h.db() as session:
        team = await session.get_one(models.Project, project.id)
        assert team.updated_at == unchanged


async def test_autofill_waits_for_head_start(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project, _ = await seed_autofill_team(h, event, seat_open_since=h.now - timedelta(hours=1))
    await enroll(h, event, await h.user(), status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 0
    assert await count(h, models.ProjectMember, models.ProjectMember.project_id == project.id) == 2
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_autofill_prioritizes_below_min_over_older_seats(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=3, team_size_max=5)
    await set_phase(h, event, "locked")
    first, _ = await seed_autofill_team(
        h, event, size=1, name="Below minimum", seat_open_since=h.now - timedelta(hours=20)
    )
    await seed_autofill_team(
        h, event, size=3, name="At minimum", seat_open_since=h.now - timedelta(hours=30)
    )
    user = await h.user()
    await enroll(h, event, user, status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 1
    async with h.db() as session:
        member = await session.scalar(
            select(models.ProjectMember).where(models.ProjectMember.user_id == user.id)
        )
        assert member is not None
        assert member.project_id == first.id


async def test_autofill_one_offer_per_member_across_teams(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    first, _ = await seed_autofill_team(
        h, event, size=4, name="First", seat_open_since=h.now - timedelta(hours=14)
    )
    second, _ = await seed_autofill_team(
        h, event, size=4, name="Second", seat_open_since=h.now - timedelta(hours=13)
    )
    first_user = await h.user()
    await enroll(h, event, first_user, status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 1
    assert (
        await count(h, models.ProjectMember, models.ProjectMember.joined_via == "reserve_offer")
        == 1
    )
    second_user = await h.user()
    await enroll(h, event, second_user, status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 1
    async with h.db() as session:
        offers = {
            member.user_id: member.project_id
            for member in await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.joined_via == "reserve_offer"
                )
            )
        }
    assert offers == {first_user.id: first.id, second_user.id: second.id}
    assert await count(h, models.Notification, models.Notification.type == "reserve_offer") == 2


async def test_autofill_excludes_departed_banned_and_dnd(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    first, _ = await seed_autofill_team(
        h, event, size=4, name="First", seat_open_since=h.now - timedelta(hours=14)
    )
    second, _ = await seed_autofill_team(
        h, event, size=4, name="Second", seat_open_since=h.now - timedelta(hours=13)
    )
    departed = await h.user()
    await enroll(h, event, departed, status="reserve", reserve_rank=1)
    old_member = await add_member(h, event, first, departed)
    async with h.db() as session:
        row = await session.get_one(models.ProjectMember, old_member.id)
        row.left_at = h.now - timedelta(hours=1)
        row.left_reason = "declined"
    banned, dnd = await h.user(banned_at=h.now), await h.user(dnd_indefinite=True)
    for rank, user in enumerate((banned, dnd), start=2):
        await enroll(h, event, user, status="reserve", reserve_rank=rank)
    assert await run_autofill(h, event) == 1
    async with h.db() as session:
        members = list(
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.joined_via == "reserve_offer"
                )
            )
        )
    assert len(members) == 1
    assert (members[0].user_id, members[0].project_id) == (departed.id, second.id)
    assert not await notifications_for(h, banned, "reserve_offer")
    assert not await notifications_for(h, dnd, "reserve_offer")


async def test_autofill_uses_recommendation_order(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project, _ = await seed_autofill_team(
        h, event, size=4, seat_open_since=h.now - timedelta(hours=13)
    )
    a, c = await h.user(), await h.user()
    await enroll(h, event, a, status="reserve", reserve_rank=1)
    await enroll(h, event, c, status="reserve", reserve_rank=2)
    async with h.db() as session:
        session.add(
            models.RecommendationSet(
                subject_type="project",
                subject_id=project.id,
                status="ready",
                input_hash="x",
                algo_version="fake-1",
                created_at=h.now,
                enrolled=[
                    {
                        "user_id": str(user.id),
                        "score": 0.9,
                        "reasons": ["Strong match"],
                        "tags": [],
                        "suggested_area": "backend",
                    }
                    for user in (c, a)
                ],
            )
        )
    assert await run_autofill(h, event) == 1
    [notification] = await notifications_for(h, c, "reserve_offer")
    payload = cast(dict[str, Any], notification.payload)
    assert payload["rank"] == 1
    assert not await notifications_for(h, a, "reserve_offer")


async def test_autofill_deadline_clamp_and_stop(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    deadline = h.now + timedelta(hours=2)
    event = await make_hackathon(
        h, team_size_min=2, team_size_max=5, registration_deadline=deadline
    )
    await set_phase(h, event, "locked")
    await seed_autofill_team(h, event, seat_open_since=h.now - timedelta(hours=13))
    user = await h.user()
    await enroll(h, event, user, status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 1
    async with h.db() as session:
        member = await session.scalar(
            select(models.ProjectMember).where(models.ProjectMember.user_id == user.id)
        )
        assert member is not None
        assert member.confirm_by == deadline
    late = await h.user()
    await enroll(h, event, late, status="reserve", reserve_rank=1)
    pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    h.advance(timedelta(hours=2))
    assert await run_autofill(h, event) == 0
    assert await count(h, models.ProjectMember, models.ProjectMember.user_id == late.id) == 0
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before


async def test_reserve_offer_confirmation_notifies_lead_once(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project, lead = await seed_autofill_team(
        h, event, size=4, seat_open_since=h.now - timedelta(hours=13)
    )
    offered = await h.user()
    await enroll(h, event, offered, status="reserve", reserve_rank=1)
    assert await run_autofill(h, event) == 1
    assert not await notifications_for(h, lead, "seat_autofilled")
    pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    audits_before = await count(h, models.AuditLog)
    async with await h.client(offered) as client:
        path = f"/api/v1/projects/{project.id}/placement/confirm"
        response = await client.post(path)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "confirmed"
        [notification] = await notifications_for(h, lead, "seat_autofilled")
        payload = cast(dict[str, Any], notification.payload)
        assert payload["member"]["user_id"] == str(offered.id)
        assert payload["open_seats"] == 0
        h.advance(timedelta(minutes=1))
        repeated = await client.post(path)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == response.json()
    assert len(await notifications_for(h, lead, "seat_autofilled")) == 1
    assert await count(h, models.AuditLog) == audits_before
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before


async def test_autofill_sweep_starts_head_start(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    project, lead = await seed_autofill_team(h, event)
    user = await h.user()
    await enroll(h, event, user, status="reserve", reserve_rank=1)
    opened_at = h.now
    assert await run_autofill(h, event) == 0
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        assert row.seat_open_since == opened_at
    assert len(await notifications_for(h, lead, "seat_opened")) == 1
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]
    h.advance(timedelta(hours=13))
    assert await run_autofill(h, event) == 1
    assert len(await notifications_for(h, lead, "seat_opened")) == 1


async def test_autofill_phase_publication_and_closed_seats_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    project, _ = await seed_autofill_team(h, event, seat_open_since=h.now - timedelta(hours=13))
    await enroll(h, event, await h.user(), status="reserve", reserve_rank=1)
    for phase in ("open", "forming", "review", "closed"):
        await set_phase(h, event, phase)
        assert await run_autofill(h, event) == 0
    await set_phase(h, event, "locked")
    for status in ("draft", "cancelled"):
        async with h.db() as session:
            row = await session.get_one(models.Event, event.id)
            row.status = status
        assert await run_autofill(h, event) == 0
    async with h.db() as session:
        row = await session.get_one(models.Event, event.id)
        row.status = "published"
        team = await session.get_one(models.Project, project.id)
        team.seats_closed_at = h.now
    assert await run_autofill(h, event) == 0
    assert await count(h, models.ProjectMember, models.ProjectMember.project_id == project.id) == 2
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def run_registration_cutoff(h: Harness, event: models.Event) -> int:
    async with h.db() as session:
        row = await session.scalar(
            select(models.Event).where(models.Event.id == event.id).with_for_update()
        )
        assert row is not None
        return await formation.registration_cutoff(session, h.jobs, row, h.now)


async def test_registration_cutoff_disbands_and_repeats_without_writes(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h,
        team_size_min=3,
        team_size_max=5,
        registration_deadline=h.now + timedelta(hours=1),
    )
    await set_phase(h, event, "locked")
    project, lead = await seed_autofill_team(h, event, seat_open_since=h.now - timedelta(hours=1))
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        row.ready_at = h.now
    h.advance(timedelta(hours=2))
    assert await run_registration_cutoff(h, event) == 1
    async with h.db() as session:
        team = await session.get_one(models.Project, project.id)
        assert (team.team_status, team.status, team.lead_user_id) == (
            "disbanded",
            "disbanded",
            None,
        )
        assert team.ready_at is None
        assert team.seat_open_since is None
        assert team.last_lead_action_at == h.now
        members = list(
            await session.scalars(
                select(models.ProjectMember).where(models.ProjectMember.project_id == project.id)
            )
        )
        assert len(members) == 2
        for member in members:
            assert (member.left_at, member.left_reason) == (h.now, "disbanded")
            enrollment = await formation.live_enrollment_row(session, event.id, member.user_id)
            assert enrollment is not None
            assert (enrollment.status, enrollment.reserve_rank, enrollment.status_at) == (
                "reserve",
                None,
                h.now,
            )
        marker = await formation.formation_row(session, event.id)
        assert marker.reg_cutoff_at == h.now
        assert marker.late_round_at is None
        before = (team.updated_at, marker.updated_at)
        audits = list(await session.scalars(select(models.AuditLog)))
        alerts = list(await session.scalars(select(models.AdminAlert)))
        users = list(
            await session.scalars(
                select(models.User).where(
                    models.User.id.in_([member.user_id for member in members])
                )
            )
        )
    for user in users:
        [notification] = await notifications_for(h, user, "team_disbanded")
        assert notification.dedupe_key == f"team_disbanded:{project.id}:{user.id}"
        payload = cast(dict[str, Any], notification.payload)
        assert (
            payload["reason"] == "The team was below the minimum size at the registration deadline"
        )
    assert await count(h, models.Notification) == 2
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.action, audit.actor_kind, audit.actor_id, audit.entity_type, audit.entity_id) == (
        "project_disband",
        "system",
        None,
        "project",
        str(project.id),
    )
    assert audit.before == {"team_status": "locked", "lead_user_id": str(lead.id)}
    assert audit.after == {"team_status": "disbanded", "reason": "below_min_at_reg_deadline"}
    assert len(alerts) == 1
    assert (alerts[0].status, alerts[0].severity, alerts[0].subject_id) == (
        "open",
        "warning",
        project.id,
    )
    assert alerts[0].dedupe_key == f"team_below_min:{project.id}"
    pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert [job["task_name"] for job in pending_before] == ["rank_reserve"]
    h.advance(timedelta(hours=1))
    assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        team = await session.get_one(models.Project, project.id)
        marker = await formation.formation_row(session, event.id)
        assert (team.updated_at, marker.updated_at) == before
    assert await count(h, models.Notification) == 2
    assert await count(h, models.AuditLog) == 1
    assert await count(h, models.AdminAlert) == 1
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before


async def test_registration_cutoff_preserves_below_min_with_pending_offer(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    deadline = h.now + timedelta(hours=1)
    event = await make_hackathon(
        h, team_size_min=3, team_size_max=5, registration_deadline=deadline
    )
    await set_phase(h, event, "locked")
    project, _ = await seed_autofill_team(h, event, size=1)
    await add_member(
        h,
        event,
        project,
        await h.user(),
        state="proposed",
        joined_via="reserve_offer",
        confirm_kind="placement",
        confirm_by=deadline,
    )
    h.advance(timedelta(hours=2))
    assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        team = await session.get_one(models.Project, project.id)
        assert team.team_status == "locked"
        marker = await formation.formation_row(session, event.id)
        assert marker.reg_cutoff_at == h.now
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
            == 2
        )
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AdminAlert) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_registration_cutoff_before_deadline_does_not_set_marker(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h, team_size_min=3, team_size_max=5, registration_deadline=h.now + timedelta(hours=1)
    )
    await set_phase(h, event, "locked")
    project, _ = await seed_autofill_team(h, event)
    assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        marker = await formation.formation_row(session, event.id)
        assert marker.reg_cutoff_at is None
        assert (await session.get_one(models.Project, project.id)).team_status == "locked"
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AdminAlert) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_registration_cutoff_keeps_later_droppers(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h, team_size_min=2, team_size_max=5, registration_deadline=h.now + timedelta(hours=1)
    )
    await set_phase(h, event, "locked")
    project, lead = await seed_autofill_team(h, event, size=3)
    h.advance(timedelta(hours=2))
    assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        marker = await formation.formation_row(session, event.id)
        assert marker.reg_cutoff_at == h.now
        for member in await session.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id != lead.id,
            )
        ):
            member.left_at = h.now
            member.left_reason = "left"
    await open_project_seat(h, event, project)
    assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        team = await session.get_one(models.Project, project.id)
        assert team.team_status == "locked"
        alert = await session.scalar(select(models.AdminAlert))
        assert alert is not None
        assert alert.status == "open"
        assert alert.dedupe_key == f"team_below_min:{project.id}"
    assert await count(h, models.AdminAlert) == 1
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_registration_cutoff_phase_and_publication_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(
        h, team_size_min=3, team_size_max=5, registration_deadline=h.now + timedelta(hours=1)
    )
    project, _ = await seed_autofill_team(h, event)
    h.advance(timedelta(hours=2))
    for phase in ("open", "forming", "review", "closed"):
        await set_phase(h, event, phase)
        assert await run_registration_cutoff(h, event) == 0
    await set_phase(h, event, "locked")
    for status in ("draft", "cancelled"):
        async with h.db() as session:
            row = await session.get_one(models.Event, event.id)
            row.status = status
        assert await run_registration_cutoff(h, event) == 0
    async with h.db() as session:
        marker = await formation.formation_row(session, event.id)
        assert marker.reg_cutoff_at is None
        assert (await session.get_one(models.Project, project.id)).team_status == "locked"
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AdminAlert) == 0
    assert await count(h, models.AuditLog) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_registration_cutoff_closes_scoped_work_and_refreshes_survivor(
    harness: Harness,
) -> None:
    h = harness
    await h.seed_areas()
    deadline = h.now + timedelta(hours=1)
    event = await make_hackathon(
        h, team_size_min=3, team_size_max=5, registration_deadline=deadline
    )
    await set_phase(h, event, "locked")
    small, small_lead = await seed_autofill_team(h, event, name="Small")
    survivor, survivor_lead = await seed_autofill_team(h, event, size=3, name="Survivor")
    outsiders = [await h.user() for _ in range(2)]
    async with h.db() as session:
        invites = [
            models.Invite(
                project_id=project.id,
                event_id=event.id,
                user_id=outsider.id,
                invited_by=lead.id,
                kind="fill",
                status="pending",
                expires_at=deadline,
            )
            for project, lead, outsider in (
                (small, small_lead, outsiders[0]),
                (survivor, survivor_lead, outsiders[1]),
            )
        ]
        requests = [
            models.JoinRequest(
                project_id=project.id, event_id=event.id, user_id=outsider.id, status="pending"
            )
            for project, outsider in ((small, outsiders[0]), (survivor, outsiders[1]))
        ]
        offer = models.LeadOffer(
            project_id=small.id,
            event_id=event.id,
            user_id=small_lead.id,
            source="succession",
            reason="Pending offer",
            status="pending",
            expires_at=deadline,
        )
        swaps = [
            models.SwapRequest(
                event_id=event.id,
                from_team_id=source.id,
                to_team_id=target.id,
                from_user_id=source_lead.id,
                to_user_id=target_lead.id,
                requested_by=source_lead.id,
                status="pending",
            )
            for source, source_lead, target, target_lead in (
                (small, small_lead, survivor, survivor_lead),
                (survivor, survivor_lead, small, small_lead),
            )
        ]
        merges = [
            models.MergeProposal(
                event_id=event.id,
                absorbed_team_id=absorbed.id,
                target_team_id=target.id,
                proposed_by="lead",
                status="pending",
            )
            for absorbed, target in ((small, survivor), (survivor, small))
        ]
        session.add_all([*invites, *requests, offer, *swaps, *merges])
        await session.flush()
    h.advance(timedelta(hours=2))
    assert await run_registration_cutoff(h, event) == 1
    async with h.db() as session:
        closed_invite = await session.get_one(models.Invite, invites[0].id)
        assert (closed_invite.status, closed_invite.closed_reason, closed_invite.responded_at) == (
            "closed",
            "team_disbanded",
            h.now,
        )
        assert (await session.get_one(models.Invite, invites[1].id)).status == "pending"
        assert (await session.get_one(models.JoinRequest, requests[0].id)).status == "closed"
        assert (await session.get_one(models.JoinRequest, requests[1].id)).status == "pending"
        assert (await session.get_one(models.LeadOffer, offer.id)).status == "closed"
        for swap in swaps:
            assert (await session.get_one(models.SwapRequest, swap.id)).status == "closed"
        for merge in merges:
            assert (await session.get_one(models.MergeProposal, merge.id)).status == "closed"
        assert (await session.get_one(models.Project, survivor.id)).team_status == "locked"
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert sorted(job["task_name"] for job in pending) == [
        "rank_reserve",
        "refresh_recommendations",
    ]
    refresh = next(job for job in pending if job["task_name"] == "refresh_recommendations")
    assert refresh["args"]["project_id"] == str(survivor.id)
    assert not await notifications_for(h, survivor_lead, "team_disbanded")


async def seed_late_lead_fit(
    h: Harness, event: models.Event, user: models.User, fit: float = 0.9
) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(str(fit)),
                reason="Stored lead fit",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def run_late(h: Harness, event: models.Event) -> bool:
    async with h.db() as session:
        row = await session.scalar(
            select(models.Event).where(models.Event.id == event.id).with_for_update()
        )
        assert row is not None
        state = await formation.formation_row(session, event.id)
        return await formation.run_late_round(session, h.jobs, row, state, h.now)


async def seed_late_core(
    h: Harness,
    event: models.Event,
    *,
    size: int = 2,
    age: timedelta = timedelta(hours=1),
    name: str = "Late core",
) -> tuple[models.Project, models.User]:
    project = await seed_project(
        h, event, name=name, team_status="forming", status="idea", origin="late_claim"
    )
    lead = await h.user()
    await add_member(h, event, project, lead, role="lead", set_lead=True)
    for _ in range(size - 1):
        await add_member(h, event, project, await h.user())
    async with h.db() as session:
        row = await session.get_one(models.Project, project.id)
        row.created_at = h.now - age
    return project, lead


async def test_late_round_opportunities_and_daily_dedupe(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    await seed_autofill_team(h, event, size=5)
    users = [await h.user() for _ in range(4)]
    for index, user in enumerate(users):
        await enroll(h, event, user, status="reserve", reserve_rank=index + 1)
        await seed_late_lead_fit(h, event, user, 0.2 if index == 3 else 0.9)
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        assert state.late_round_at is None
    opened_at = h.now
    assert await run_late(h, event) is False
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        assert state.late_round_at == opened_at
    for user in users[:3]:
        [notification] = await notifications_for(h, user, "lead_opportunity")
        payload = cast(dict[str, Any], notification.payload)
        assert datetime.fromisoformat(payload["expires_at"]) == h.now + timedelta(hours=24)
        assert payload["reserve_count"] == 4
        assert payload["viable_size"] == 4
        key = f"lead_opportunity:{event.id}:{h.now.date().isoformat()}:{user.id}"
        assert notification.dedupe_key == key
    assert not await notifications_for(h, users[3], "lead_opportunity")
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert await run_late(h, event) is False
    assert await count(h, models.Notification) == 3
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        assert state.late_round_at == opened_at
    h.advance(timedelta(days=1))
    assert await run_late(h, event) is True
    for user in users[:3]:
        assert len(await notifications_for(h, user, "lead_opportunity")) == 2
    assert await count(h, models.AuditLog) == 0


async def test_late_round_grace_expiry_and_marker_restart(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked", late_round_at=h.now - timedelta(hours=25))
    await seed_autofill_team(h, event, size=5)
    for _ in range(4):
        user = await h.user()
        await enroll(h, event, user, status="reserve")
        await seed_late_lead_fit(h, event, user)
    assert await run_late(h, event) is True
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert len(pending) == 1
    assert pending[0]["task_name"] == "run_formation"
    assert pending[0]["args"] == {"event_id": str(event.id), "kind": "late"}
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at is None
    notices = await count(h, models.Notification)
    assert await run_late(h, event) is False
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at == h.now
    assert await count(h, models.Notification) == notices
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending


async def test_late_round_open_seat_blocks_and_clears_marker(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked", late_round_at=h.now - timedelta(hours=1))
    project, _ = await seed_autofill_team(h, event, size=4)
    for _ in range(4):
        user = await h.user()
        await enroll(h, event, user, status="reserve")
        await seed_late_lead_fit(h, event, user)
    assert await run_late(h, event) is False
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at is None
        row = await session.get_one(models.Project, project.id)
        row.seats_closed_at = h.now
    assert await count(h, models.Notification) == 0
    assert await run_late(h, event) is False
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at == h.now
    assert await count(h, models.Notification) == 4


async def test_late_round_core_window_and_full_core(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked", late_round_at=h.now)
    await seed_late_core(h, event, age=timedelta(hours=13))
    assert await run_late(h, event) is True
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at is None
    other = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, other, "locked", late_round_at=h.now - timedelta(hours=25))
    core, _ = await seed_late_core(h, other)
    pending_before = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert await run_late(h, other) is False
    assert [job for job in await h.pending_jobs() if job["status"] == "todo"] == pending_before
    await add_member(h, other, core, await h.user())
    assert await run_late(h, other) is True
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert len(pending) == 2
    assert all(
        job["task_name"] == "run_formation" and job["args"]["kind"] == "late" for job in pending
    )
    assert await count(h, models.Notification) == 0


async def test_late_round_waits_for_every_core(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked", late_round_at=h.now)
    await seed_late_core(h, event, name="Ready", age=timedelta(hours=13))
    building, _ = await seed_late_core(h, event, name="Building", age=timedelta(hours=1))
    assert await run_late(h, event) is False
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]
    await add_member(h, event, building, await h.user())
    assert await run_late(h, event) is True
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert len(pending) == 1
    assert pending[0]["task_name"] == "run_formation"
    assert pending[0]["args"] == {"event_id": str(event.id), "kind": "late"}
    assert await count(h, models.Notification) == 0


async def test_late_claim_eligibility_and_gates_without_notification(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked", late_round_at=h.now)
    user = await h.user()
    enrollment = await enroll(h, event, user, status="reserve")
    await seed_late_lead_fit(h, event, user)
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=user.id, type="lead_opportunity", web=False, telegram=False
            )
        )
        state = await formation.formation_row(session, event.id)
        assert await formation.late_claim_eligible(session, event, user.id) is True
        assert await formation.late_claim_open(session, event, state, user.id, h.now) is True
        state.late_round_at = None
        assert await formation.late_claim_open(session, event, state, user.id, h.now) is False
        state.late_round_at = h.now
        horizon = event.starts_at - timedelta(hours=12)
        assert await formation.late_claim_open(session, event, state, user.id, horizon) is False
        for phase in ("review", "closed"):
            state.phase = phase
            assert await formation.late_claim_open(session, event, state, user.id, h.now) is False
        state.phase = "locked"
        fit = await session.get_one(models.LeadFit, (event.id, user.id))
        fit.fit = Decimal("0.2")
        assert await formation.late_claim_open(session, event, state, user.id, h.now) is False
        fit.fit = Decimal("0.9")
        row = await session.get_one(models.Enrollment, enrollment.id)
        row.status = "withdrawn"
        assert await formation.late_claim_open(session, event, state, user.id, h.now) is False
    assert not await notifications_for(h, user, "lead_opportunity")


async def test_late_claim_end_to_end_with_notifications_disabled(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    await seed_autofill_team(h, event, size=5)
    normal, muted, low, extra = [await h.user() for _ in range(4)]
    for user, fit in ((normal, 0.9), (muted, 0.9), (low, 0.2), (extra, 0.2)):
        await enroll(h, event, user, status="reserve")
        await seed_late_lead_fit(h, event, user, fit)
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=muted.id, type="lead_opportunity", web=False, telegram=False
            )
        )
    assert await run_late(h, event) is False
    assert len(await notifications_for(h, normal, "lead_opportunity")) == 1
    assert not await notifications_for(h, muted, "lead_opportunity")
    async with await h.client(low) as client:
        response = await client.post(
            f"/api/v1/events/{event.id}/projects", json={"name": "Ineligible"}
        )
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    for user in (normal, muted):
        async with await h.client(user) as client:
            response = await client.post(
                f"/api/v1/events/{event.id}/projects", json={"name": f"Late core {user.id}"}
            )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["origin"] == "late_claim"
        assert data["team_status"] == "forming"
        assert data["lead_user_id"] == str(user.id)
        async with h.db() as session:
            row = await formation.live_enrollment_row(session, event.id, user.id)
            assert row is not None and row.status == "placed"
            audits = list(
                await session.scalars(
                    select(models.AuditLog).where(models.AuditLog.actor_id == user.id)
                )
            )
            assert len(audits) == 1
            assert audits[0].action == "lead_assign"
            assert cast(dict[str, Any], audits[0].after)["origin"] == "late_claim"


async def test_late_core_open_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    core, _ = await seed_late_core(h, event, size=1, age=timedelta())
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        project = await session.get_one(models.Project, core.id)
        assert await formation.late_core_open(session, event, state, project, h.now) is True
        project.origin = "claim"
        assert await formation.late_core_open(session, event, state, project, h.now) is False
        project.origin = "late_claim"
        project.created_at = h.now - timedelta(hours=13)
        assert await formation.late_core_open(session, event, state, project, h.now) is False
        project.created_at = h.now
        state.phase = "review"
        assert await formation.late_core_open(session, event, state, project, h.now) is False
        state.phase = "locked"
        project.team_status = "locked"
        assert await formation.late_core_open(session, event, state, project, h.now) is False
        project.team_status = "forming"
        horizon = event.starts_at - timedelta(hours=12)
        assert await formation.late_core_open(session, event, state, project, horizon) is False
    for _ in range(2):
        await add_member(h, event, core, await h.user())
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        project = await session.get_one(models.Project, core.id)
        assert await formation.late_core_open(session, event, state, project, h.now) is False


async def test_late_core_invite_path_is_enabled(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    core, lead = await seed_late_core(h, event, size=1, age=timedelta())
    target = await h.user()
    await enroll(h, event, target, status="reserve")
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{core.id}/invites", json={"user_id": str(target.id)}
        )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "core"
    ordinary = await seed_project(h, event, team_status="forming", status="idea", origin="claim")
    ordinary_lead = await h.user()
    await add_member(h, event, ordinary, ordinary_lead, role="lead", set_lead=True)
    async with await h.client(ordinary_lead) as client:
        response = await client.post(
            f"/api/v1/projects/{ordinary.id}/invites", json={"user_id": str(target.id)}
        )
        assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_late_round_reserve_time_publication_and_phase_gates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await seed_autofill_team(h, event, size=5)
    for _ in range(3):
        user = await h.user()
        await enroll(h, event, user, status="reserve")
        await seed_late_lead_fit(h, event, user)
    await set_phase(h, event, "locked", late_round_at=h.now - timedelta(hours=1))
    assert await run_late(h, event) is False
    async with h.db() as session:
        assert (await formation.formation_row(session, event.id)).late_round_at is None
    fourth = await h.user()
    await enroll(h, event, fourth, status="reserve")
    await seed_late_lead_fit(h, event, fourth)
    for phase in ("open", "review", "forming", "closed"):
        await set_phase(h, event, phase)
        assert await run_late(h, event) is False
    await set_phase(h, event, "locked")
    async with h.db() as session:
        row = await session.get_one(models.Event, event.id)
        row.status = "cancelled"
    assert await run_late(h, event) is False
    async with h.db() as session:
        row = await session.get_one(models.Event, event.id)
        row.status = "published"
    h.advance(timedelta(days=29, hours=12))
    assert await run_late(h, event) is False
    assert await count(h, models.Notification) == 0
    assert not [job for job in await h.pending_jobs() if job["status"] == "todo"]


async def test_late_opportunity_excludes_dnd_banned_and_unonboarded(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, "locked")
    await seed_autofill_team(h, event, size=5)
    eligible = await h.user()
    dnd = await h.user(dnd_indefinite=True)
    banned = await h.user(banned_at=h.now)
    unfinished = await h.user(onboarded=False)
    missing_fit = await h.user()
    for user in (eligible, dnd, banned, unfinished, missing_fit):
        await enroll(h, event, user, status="reserve")
        if user is not missing_fit:
            await seed_late_lead_fit(h, event, user)
    assert await run_late(h, event) is False
    assert len(await notifications_for(h, eligible, "lead_opportunity")) == 1
    for user in (dnd, banned, unfinished, missing_fit):
        assert not await notifications_for(h, user, "lead_opportunity")
    async with h.db() as session:
        for user in (banned, unfinished, missing_fit):
            assert await formation.late_claim_eligible(session, event, user.id) is False


async def test_late_core_invite_expiry_uses_capped_window_not_main_formation(
    harness: Harness,
) -> None:
    h = harness
    await h.seed_areas()
    for deadline_hours in (48, 16):
        deadline = h.now + timedelta(hours=deadline_hours)
        event = await make_hackathon(
            h, team_size_min=2, team_size_max=5, registration_deadline=deadline
        )
        await set_phase(h, event, "locked", formation_at=h.now - timedelta(days=1))
        core, lead = await seed_late_core(h, event, size=1, age=timedelta())
        target = await h.user()
        await enroll(h, event, target, status="reserve")
        expected = min(h.now + timedelta(hours=12), deadline - timedelta(hours=12))
        async with await h.client(lead) as client:
            response = await client.post(
                f"/api/v1/projects/{core.id}/invites", json={"user_id": str(target.id)}
            )
        assert response.status_code == 201, response.text
        assert datetime.fromisoformat(response.json()["expires_at"]) == expected
        async with h.db() as session:
            invite = await session.scalar(
                select(models.Invite).where(models.Invite.project_id == core.id)
            )
            assert invite is not None
            assert invite.expires_at == expected
            assert invite.expires_at > h.now
    main = await make_hackathon(h, team_size_min=2, team_size_max=5)
    main_formation_at = h.now + timedelta(hours=2)
    await set_phase(h, main, "open", formation_at=main_formation_at)
    core = await seed_project(h, main, team_status="forming", status="idea", origin="claim")
    lead, target = await h.user(), await h.user()
    await add_member(h, main, core, lead, role="lead", set_lead=True)
    async with await h.client(lead) as client:
        response = await client.post(
            f"/api/v1/projects/{core.id}/invites", json={"user_id": str(target.id)}
        )
    assert response.status_code == 201, response.text
    assert datetime.fromisoformat(response.json()["expires_at"]) == main_formation_at


async def test_late_core_readiness_uses_registration_capped_window(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    deadline = h.now + timedelta(hours=13)
    event = await make_hackathon(
        h, team_size_min=2, team_size_max=5, registration_deadline=deadline
    )
    await set_phase(h, event, "locked", formation_at=h.now - timedelta(days=1))
    core, _ = await seed_late_core(h, event, size=1, age=timedelta())
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        project = await session.get_one(models.Project, core.id)
        assert await formation.late_core_open(session, event, state, project, h.now) is True
    assert await run_late(h, event) is False
    h.advance(timedelta(hours=1))
    async with h.db() as session:
        state = await formation.formation_row(session, event.id)
        project = await session.get_one(models.Project, core.id)
        assert await formation.late_core_open(session, event, state, project, h.now) is False
    assert await run_late(h, event) is True
    pending = [job for job in await h.pending_jobs() if job["status"] == "todo"]
    assert len(pending) == 1
    assert pending[0]["args"] == {"event_id": str(event.id), "kind": "late"}
