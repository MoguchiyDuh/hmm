import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import AlgoSettings, FormationPlan, FormationRequest
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import runs, service
from hmm_core.platform.settings import load_settings
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
    areas: Sequence[tuple[str, AreaImportance]] = (("backend", AreaImportance.PRIMARY),),
) -> models.Event:
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Admin Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=team_size_min,
                team_size_max=team_size_max,
                formation_enabled=True,
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
        await service.ensure_formation(session, event, h.now)
        return event


async def set_phase(
    h: Harness, event: models.Event, phase: FormationPhase, *, rounds: int = 0
) -> models.EventFormation:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value, rounds=rounds)
        )
        formation = await session.get_one(models.EventFormation, event.id)
        await session.refresh(formation)
        return formation


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "active",
    willing: bool = False,
    area: models.Area | None = None,
    created_at: datetime | None = None,
) -> models.Enrollment:
    async with h.db() as session:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=willing,
            preferred_area_id=area.id if area is not None else None,
            status_at=h.now,
            note="",
        )
        session.add(enrollment)
        await session.flush()
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(created_at=created_at or h.now)
        )
        await session.refresh(enrollment)
        return enrollment


async def make_core(
    h: Harness,
    event: models.Event,
    name: str,
    *,
    lead: models.User | None = None,
    members: Sequence[models.User] = (),
    team_status: str = "forming",
    origin: str = "claim",
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
            lead_user_id=lead.id if lead is not None else None,
            status="idea",
            team_status=team_status,
            origin=origin,
        )
        session.add(project)
        await session.flush()
        people: list[tuple[models.User, str]] = [(lead, "lead")] if lead is not None else []
        people.extend((member, "member") for member in members)
        for user, role in people:
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role=role,
                    state="confirmed",
                    trust="self",
                    joined_via="claim",
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
            if await service.live_enrollment_row(session, event.id, user.id) is None:
                session.add(
                    models.Enrollment(
                        event_id=event.id,
                        user_id=user.id,
                        status="placed",
                        source="self",
                        willing_to_lead=False,
                        status_at=h.now,
                        note="",
                        created_at=h.now,
                    )
                )
            await session.flush()
        await session.refresh(project)
        return project


async def ensure_areas(h: Harness) -> None:
    async with h.db() as session:
        seeded = await session.scalar(select(models.Area.id).limit(1))
    if seeded is None:
        await h.seed_areas()


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def load_formation(h: Harness, event: models.Event) -> models.EventFormation:
    async with h.db() as session:
        return await session.get_one(models.EventFormation, event.id)


async def todo_jobs(h: Harness, task_name: str) -> list[dict[str, Any]]:
    return [job for job in await h.pending_jobs(task_name) if job["status"] == "todo"]


async def audits(h: Harness, action: str) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def rescheduled(h: Harness) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == "formation_rescheduled"
                )
            )
        )


async def create_event(
    h: Harness, *, kind: EventKind, status: EventStatus, formation_enabled: bool
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=kind,
                title=f"Admin Other {next(_titles)}",
                starts_at=h.now + timedelta(days=30),
                ends_at=h.now + timedelta(days=31),
                timezone="UTC",
                formation_enabled=formation_enabled,
            ),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=None,
            now=h.now,
        )


def patch_url(event: models.Event) -> str:
    return f"/api/v1/admin/events/{event.id}/formation"


def run_url(event: models.Event) -> str:
    return f"/api/v1/admin/events/{event.id}/formation/run"


def runs_url(event: models.Event) -> str:
    return f"/api/v1/admin/events/{event.id}/formation/runs"


async def open_event(h: Harness) -> models.Event:
    await ensure_areas(h)
    return await make_hackathon(h, team_size_min=3, team_size_max=6)


async def test_patch_target_override(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        response = await c.patch(
            patch_url(event), json={"target_override": 4, "reason": "smaller teams"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["target"], body["target_override"], body["viable"]) == (4, 4, 3)
    assert (await load_formation(h, event)).target_override == 4
    [audit] = await audits(h, "formation_edit")
    assert (audit.actor_kind, audit.reason, audit.actor_id) == ("admin", "smaller teams", admin.id)
    assert isinstance(audit.before, dict) and audit.before["target_override"] is None
    assert await count(h, models.Notification) == 0
    assert await h.pending_jobs() == []


async def test_patch_validation(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        for payload in (
            {"target_override": 2},
            {"target_override": 7},
            {"formation_at_override": (h.now - timedelta(hours=1)).isoformat()},
        ):
            response = await c.patch(patch_url(event), json=payload)
            assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    formation = await load_formation(h, event)
    assert (formation.target_override, formation.formation_at_override) == (None, None)
    assert await audits(h, "formation_edit") == []


async def test_patch_gates(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    review = await open_event(h)
    await set_phase(h, review, FormationPhase.REVIEW)
    forming = await make_hackathon(h)
    await set_phase(h, forming, FormationPhase.FORMING)
    plain = await create_event(
        h, kind=EventKind.MEETUP, status=EventStatus.PUBLISHED, formation_enabled=False
    )
    target = await make_hackathon(h)
    payload = {"target_override": 3}
    async with await h.client(admin) as c:
        assert_problem(await c.patch(patch_url(review), json=payload), 409, ErrorCode.INVALID_STATE)
        assert_problem(
            await c.patch(patch_url(forming), json=payload), 409, ErrorCode.FORMATION_IN_PROGRESS
        )
        assert_problem(
            await c.patch(f"/api/v1/admin/events/{uuid4()}/formation", json=payload),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(await c.patch(patch_url(plain), json=payload), 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        assert_problem(
            await c.patch(patch_url(target), json=payload), 401, ErrorCode.UNAUTHENTICATED
        )
    async with await h.client(await h.user()) as c:
        assert_problem(await c.patch(patch_url(target), json=payload), 403, ErrorCode.FORBIDDEN)
    for event in (review, forming, target):
        assert (await load_formation(h, event)).target_override is None
    assert await audits(h, "formation_edit") == []


async def test_patch_schedule_notifies(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    enrolled = [await h.user(), await h.user()]
    for user in enrolled:
        await enroll(h, event, user)
    lead = await h.user()
    await make_core(h, event, "Core", lead=lead)
    previous = (await load_formation(h, event)).formation_at
    moved = h.now + timedelta(days=10)
    admin = await h.admin()
    async with await h.client(admin) as c:
        response = await c.patch(
            patch_url(event), json={"formation_at_override": moved.isoformat()}
        )
        assert response.status_code == 200, response.text
        again = await c.patch(patch_url(event), json={"formation_at_override": moved.isoformat()})
        assert again.status_code == 200, again.text
    async with h.db() as session:
        settings = await load_settings(session)
        stored = await session.get_one(models.Event, event.id)
    team_deadline = service.formation_timing(stored, settings, h.now).team_deadline
    formation = await load_formation(h, event)
    assert formation.formation_at == moved
    assert formation.review_ends_at == min(moved + timedelta(hours=48), team_deadline)
    assert datetime.fromisoformat(response.json()["formation_at"]) == moved
    assert await h.pending_jobs() == []
    notices = await rescheduled(h)
    assert sorted(row.user_id for row in notices) == sorted(user.id for user in (*enrolled, lead))
    for row in notices:
        assert isinstance(row.payload, dict)
        assert datetime.fromisoformat(str(row.payload["previous_formation_at"])) == previous
        assert datetime.fromisoformat(str(row.payload["formation_at"])) == moved
    assert len(await audits(h, "formation_edit")) == 1


async def test_patch_small_move_is_silent(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    await enroll(h, event, await h.user())
    start = (await load_formation(h, event)).formation_at
    admin = await h.admin()
    async with await h.client(admin) as c:
        small = start + timedelta(hours=5)
        response = await c.patch(
            patch_url(event), json={"formation_at_override": small.isoformat()}
        )
        assert response.status_code == 200, response.text
        assert (await load_formation(h, event)).formation_at == small
        assert await rescheduled(h) == []
        large = small + timedelta(hours=7)
        response = await c.patch(
            patch_url(event), json={"formation_at_override": large.isoformat()}
        )
        assert response.status_code == 200, response.text
    assert len(await rescheduled(h)) == 1


async def test_patch_clears_overrides(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    computed = (await load_formation(h, event)).formation_at
    admin = await h.admin()
    async with await h.client(admin) as c:
        response = await c.patch(
            patch_url(event),
            json={
                "target_override": 4,
                "formation_at_override": (h.now + timedelta(days=10)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        response = await c.patch(patch_url(event), json={"target_override": None})
        assert response.status_code == 200, response.text
        assert (response.json()["target"], response.json()["target_override"]) == (6, None)
        response = await c.patch(patch_url(event), json={"formation_at_override": None})
        assert response.status_code == 200, response.text
    async with h.db() as session:
        settings = await load_settings(session)
        stored = await session.get_one(models.Event, event.id)
    timing = service.formation_timing(stored, settings, h.now)
    formation = await load_formation(h, event)
    assert formation.formation_at_override is None
    assert formation.formation_at == timing.team_deadline - timedelta(
        hours=settings.formation_lead_hours
    )
    assert formation.formation_at == computed


async def test_post_run_enqueues(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        response = await c.post(run_url(event), json={"reason": "now"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["status"], body["kind"], body["round"], body["planner"]) == (
        "pending",
        "main",
        1,
        "algo",
    )
    assert (body["input"], body["output"], body["algo_version"], body["applied_at"]) == (
        None,
        None,
        None,
        None,
    )
    assert body["validation_errors"] == []
    [job] = await todo_jobs(h, "run_formation")
    assert job["args"] == {"event_id": str(event.id), "run_id": body["id"], "kind": "main"}
    assert job["lock"] == f"formation:{event.id}"
    [audit] = await audits(h, "formation_run")
    assert (audit.actor_kind, audit.reason) == ("admin", "now")
    assert (await load_formation(h, event)).rounds == 0


async def test_post_run_dedupes(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        first = await c.post(run_url(event))
        second = await c.post(run_url(event))
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["id"] == second.json()["id"]
    assert len(await todo_jobs(h, "run_formation")) == 1
    assert len(await audits(h, "formation_run")) == 1


async def seed_open_event(h: Harness) -> models.Event:
    event = await open_event(h)
    for index in range(2):
        lead, member = await h.user(), await h.user()
        await make_core(h, event, f"Core {index}", lead=lead, members=[member])
    for _ in range(10):
        await enroll(h, event, await h.user())
    return event


async def test_post_run_end_to_end(harness: Harness) -> None:
    h = harness
    event = await seed_open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        assert (await c.post(run_url(event))).status_code == 202
        await h.run_jobs()
        response = await c.get(runs_url(event))
    assert response.status_code == 200, response.text
    [run] = response.json()
    assert run["status"] == "applied"
    assert run["input"]["kind"] == "main"
    assert run["output"] is not None
    assert run["planner"] in {"algo", "fallback"}
    assert run["algo_version"] == "fake-1"
    assert datetime.fromisoformat(run["applied_at"]) == h.now
    [attempt] = run["validation_errors"]
    assert (attempt["planner"], attempt["errors"], attempt["error"]) == ("algo", [], None)
    assert (await load_formation(h, event)).phase == "review"


async def test_post_run_gates(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    await ensure_areas(h)
    forming = await make_hackathon(h)
    await set_phase(h, forming, FormationPhase.FORMING)
    locked = await make_hackathon(h)
    await set_phase(h, locked, FormationPhase.LOCKED)
    closed = await make_hackathon(h)
    await set_phase(h, closed, FormationPhase.CLOSED)
    draft = await create_event(
        h, kind=EventKind.HACKATHON, status=EventStatus.DRAFT, formation_enabled=True
    )
    target = await make_hackathon(h)
    async with await h.client(admin) as c:
        assert_problem(await c.post(run_url(forming)), 409, ErrorCode.FORMATION_IN_PROGRESS)
        assert_problem(await c.post(run_url(locked)), 409, ErrorCode.INVALID_STATE)
        assert_problem(await c.post(run_url(closed)), 409, ErrorCode.INVALID_STATE)
        assert_problem(await c.post(run_url(draft)), 409, ErrorCode.INVALID_STATE)
    async with await h.client(await h.user()) as c:
        assert_problem(await c.post(run_url(target)), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        assert_problem(await c.post(run_url(target)), 401, ErrorCode.UNAUTHENTICATED)
    assert await h.pending_jobs("run_formation") == []
    assert await count(h, models.FormationRun) == 0


async def test_post_run_in_review(harness: Harness) -> None:
    h = harness
    event = await open_event(h)
    await set_phase(h, event, FormationPhase.REVIEW, rounds=1)
    await make_core(h, event, "Review", members=[await h.user()], team_status="review")
    admin = await h.admin()
    async with await h.client(admin) as c:
        response = await c.post(run_url(event))
    assert response.status_code == 202, response.text
    assert response.json()["round"] == 2
    await h.run_jobs()
    formation = await load_formation(h, event)
    assert (formation.rounds, formation.phase) == (2, "review")
    async with h.db() as session:
        run = await session.get_one(models.FormationRun, response.json()["id"])
    assert run.status == "applied"


async def test_list_runs(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    event = await seed_open_event(h)
    admin = await h.admin()
    async with await h.client(admin) as c:
        assert (await c.post(run_url(event))).status_code == 202
        await h.run_jobs()

        async def boom(ctx: Any, request: FormationRequest) -> FormationPlan:
            raise RuntimeError("boom")

        def invalid(request: FormationRequest, settings: AlgoSettings) -> FormationPlan:
            return FormationPlan(teams=[], reserve=[], merges=[], summary="Broken")

        monkeypatch.setattr(h.algorithm, "form_teams", boom)
        monkeypatch.setattr(runs, "fallback_plan", invalid)
        failed = await c.post(run_url(event))
        assert failed.status_code == 202, failed.text
        await h.run_jobs()
        response = await c.get(runs_url(event))
        missing = await c.get(f"/api/v1/admin/events/{uuid4()}/formation/runs")
        draft = await create_event(
            h, kind=EventKind.HACKATHON, status=EventStatus.DRAFT, formation_enabled=True
        )
        draft_runs = await c.get(runs_url(draft))
    assert response.status_code == 200, response.text
    newest, oldest = response.json()
    assert newest["id"] == failed.json()["id"]
    assert (newest["status"], newest["output"]) == ("failed", None)
    assert newest["validation_errors"]
    assert oldest["status"] == "applied"
    assert_problem(missing, 404, ErrorCode.NOT_FOUND)
    assert (draft_runs.status_code, draft_runs.json()) == (200, [])
    async with h.anonymous() as c:
        assert_problem(await c.get(runs_url(event)), 401, ErrorCode.UNAUTHENTICATED)
