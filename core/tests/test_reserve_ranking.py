import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import RankedMember, ReserveRequest
from hmm_contracts.common import (
    AreaImportance,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationKind,
    FormationPhase,
    FormationPlanner,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import jobs as formation_jobs
from hmm_core.formation import service as formation
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings
from sqlalchemy import func, select, update

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
                title=f"Reserve Hack {next(_titles)}",
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
        await formation.ensure_formation(session, event, h.now)
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
            if await formation.live_enrollment_row(session, event.id, user.id) is None:
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


async def add_fit(h: Harness, event: models.Event, user: models.User, fit: float) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(str(fit)),
                reason="fit",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def ensure_areas(h: Harness) -> None:
    async with h.db() as session:
        seeded = await session.scalar(select(models.Area.id).limit(1))
    if seeded is None:
        await h.seed_areas()


async def start_run(
    h: Harness, event: models.Event, kind: FormationKind = FormationKind.MAIN
) -> models.FormationRun:
    async with h.db() as session:
        formation = await session.get_one(models.EventFormation, event.id)
        run = models.FormationRun(
            event_id=event.id,
            round=formation.rounds + 1,
            kind=kind.value,
            status="running",
            planner="fallback",
            input={},
            output=None,
            validation_errors=[],
            algo_version="fake-1",
            applied_at=None,
        )
        session.add(run)
        formation.phase = FormationPhase.FORMING.value
        await session.flush()
        await session.refresh(run)
        return run


async def reserve_ranks(h: Harness, event: models.Event) -> list[tuple[UUID, int | None]]:
    async with h.db() as session:
        return [
            (row.user_id, row.reserve_rank)
            for row in await session.scalars(
                select(models.Enrollment)
                .where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.status == "reserve",
                )
                .order_by(models.Enrollment.created_at, models.Enrollment.id)
            )
        ]


async def set_ranks(
    h: Harness, enrollments: Sequence[models.Enrollment], ranks: Sequence[int]
) -> None:
    async with h.db() as session:
        for enrollment, rank in zip(enrollments, ranks, strict=True):
            await session.execute(
                update(models.Enrollment)
                .where(models.Enrollment.id == enrollment.id)
                .values(reserve_rank=rank)
            )


async def count(h: Harness, model: Any, *conditions: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def reserve_event(
    h: Harness, size: int = 3, phase: FormationPhase = FormationPhase.REVIEW
) -> tuple[models.Event, list[models.User], list[models.Enrollment]]:
    await ensure_areas(h)
    event = await make_hackathon(h)
    await set_phase(h, event, phase, rounds=1)
    users, enrollments = [], []
    for index in range(size):
        user = await h.user()
        users.append(user)
        enrollments.append(
            await enroll(
                h,
                event,
                user,
                status="reserve",
                created_at=h.now - timedelta(hours=size - index),
            )
        )
    return event, users, enrollments


async def test_rank_reserve_happy_path(harness: Harness) -> None:
    h = harness
    event, users, _ = await reserve_event(h)
    await formation_jobs.rank_reserve(h.runtime, event_id=str(event.id))
    assert await reserve_ranks(h, event) == [(user.id, rank) for rank, user in enumerate(users, 1)]
    assert await count(h, models.Notification) == 0
    assert await count(h, models.AuditLog) == 0
    assert await h.pending_jobs() == []


async def test_rank_reserve_overwrites_ranks(harness: Harness) -> None:
    h = harness
    event, users, enrollments = await reserve_event(h)
    await set_ranks(h, enrollments, [3, 1, 2])
    await formation_jobs.rank_reserve(h.runtime, event_id=str(event.id))
    assert await reserve_ranks(h, event) == [(user.id, rank) for rank, user in enumerate(users, 1)]
    async with h.db() as session:
        for enrollment in enrollments:
            stored = await session.get_one(models.Enrollment, enrollment.id)
            assert (stored.status, stored.status_at) == ("reserve", enrollment.status_at)


async def test_rank_reserve_gates(harness: Harness) -> None:
    h = harness
    for phase in (FormationPhase.OPEN, FormationPhase.CLOSED):
        event, users, _ = await reserve_event(h, phase=phase)
        await formation_jobs.rank_reserve(h.runtime, event_id=str(event.id))
        assert await reserve_ranks(h, event) == [(user.id, None) for user in users]
    async with h.db() as session:
        draft = await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Reserve Draft {next(_titles)}",
                starts_at=h.now + timedelta(days=30),
                ends_at=h.now + timedelta(days=31),
                timezone="UTC",
                formation_enabled=True,
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.DRAFT,
            created_by=None,
            now=h.now,
        )
    drafted = await h.user()
    await enroll(h, draft, drafted, status="reserve")
    await formation_jobs.rank_reserve(h.runtime, event_id=str(draft.id))
    assert await reserve_ranks(h, draft) == [(drafted.id, None)]
    empty = await make_hackathon(h)
    await set_phase(h, empty, FormationPhase.REVIEW, rounds=1)
    await enroll(h, empty, await h.user())
    await formation_jobs.rank_reserve(h.runtime, event_id=str(empty.id))
    assert await reserve_ranks(h, empty) == []
    assert await count(h, models.Notification) == 0


async def test_rank_reserve_failure_keeps_order(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    event, users, enrollments = await reserve_event(h)
    await set_ranks(h, enrollments[:2], [2, 1])

    async def boom(ctx: Any, request: ReserveRequest) -> list[RankedMember]:
        raise RuntimeError("boom")

    monkeypatch.setattr(h.algorithm, "rank_reserve", boom)
    await formation_jobs.rank_reserve(h.runtime, event_id=str(event.id))
    assert await reserve_ranks(h, event) == [(users[0].id, 2), (users[1].id, 1), (users[2].id, 3)]
    assert await count(h, models.AdminAlert) == 0


async def test_rank_reserve_validates_algo_output(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    event, users, _ = await reserve_event(h)

    async def sloppy(ctx: Any, request: ReserveRequest) -> list[RankedMember]:
        return [
            RankedMember(user_id=uuid4(), rank=1, reason="x"),
            RankedMember(user_id=users[1].id, rank=2, reason="x"),
            RankedMember(user_id=users[0].id, rank=2, reason="x"),
            RankedMember(user_id=users[0].id, rank=5, reason="x"),
        ]

    monkeypatch.setattr(h.algorithm, "rank_reserve", sloppy)
    await formation_jobs.rank_reserve(h.runtime, event_id=str(event.id))
    ranks = dict(await reserve_ranks(h, event))
    assert sorted(rank or 0 for rank in ranks.values()) == [1, 2, 3]
    assert (ranks[users[1].id], ranks[users[0].id], ranks[users[2].id]) == (1, 2, 3)

    final = formation.validated_reserve_ranking(
        [user.id for user in users],
        [
            RankedMember(user_id=uuid4(), rank=1, reason="x"),
            RankedMember(user_id=users[0].id, rank=4, reason="x"),
            RankedMember(user_id=users[1].id, rank=4, reason="x"),
        ],
    )
    assert [(entry.user_id, entry.rank) for entry in final] == [
        (users[0].id, 1),
        (users[1].id, 2),
        (users[2].id, 3),
    ]


async def test_build_reserve_request_shape(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=2, team_size_max=5)
    await set_phase(h, event, FormationPhase.REVIEW, rounds=1)
    pair = [await h.user(), await h.user()]
    trio = [await h.user(), await h.user(), await h.user()]
    await make_core(h, event, "Review", lead=pair[0], members=pair[1:], team_status="review")
    await make_core(h, event, "Locked", lead=trio[0], members=trio[1:], team_status="locked")
    async with h.db() as session:
        backend = await session.scalar(select(models.Area).where(models.Area.key == "backend"))
    willing = await h.user()
    first = await enroll(
        h,
        event,
        willing,
        status="reserve",
        willing=True,
        area=backend,
        created_at=h.now - timedelta(hours=2),
    )
    await enroll(h, event, await h.user(), status="reserve")
    async with h.db() as session:
        current = await session.get_one(models.Event, event.id)
        row = await session.get_one(models.EventFormation, event.id)
        request = await formation.build_reserve_request(session, current, row, h.now)
    assert request is not None
    assert [team.open_seats for team in request.teams] == [3, 2]
    assert [len(team.members) for team in request.teams] == [2, 3]
    assert len(request.reserve) == 2
    head = request.reserve[0]
    assert (head.user_id, head.willing_to_lead, head.preferred_area, head.enrolled_at) == (
        willing.id,
        True,
        "backend",
        first.created_at,
    )
    assert request.hackathon.id == event.id


async def test_rank_reserve_job_spec(harness: Harness) -> None:
    h = harness
    event, users, _ = await reserve_event(h)
    async with h.db() as session:
        await defer_job(session, h.jobs, JobName.RANK_RESERVE, event_id=str(event.id))
    [queued] = await h.pending_jobs("rank_reserve")
    assert (queued["lock"], queued["queueing_lock"], queued["args"]) == (
        f"reserve:{event.id}",
        f"rank_reserve:{event.id}",
        {"event_id": str(event.id)},
    )
    await h.run_jobs()
    assert await reserve_ranks(h, event) == [(user.id, rank) for rank, user in enumerate(users, 1)]


async def test_rank_reserve_cron_fans_out(harness: Harness) -> None:
    h = harness
    event, _, _ = await reserve_event(h)
    idle, _, _ = await reserve_event(h, phase=FormationPhase.OPEN)
    await formation_jobs.rank_reserve(h.runtime)
    queued = [job["args"] for job in await h.pending_jobs("rank_reserve")]
    assert queued == [{"event_id": str(event.id)}]
    assert str(idle.id) not in str(queued)


async def apply_fallback(h: Harness, event: models.Event) -> bool:
    async with h.db() as session:
        current = await session.get_one(models.Event, event.id)
        row = await session.get_one(models.EventFormation, event.id)
        request = await formation.build_formation_request(
            session, current, row, FormationKind.MAIN, FormationPhase.OPEN, h.now
        )
        settings = await load_settings(session)
    plan = formation.fallback_plan(request, settings)
    run = await start_run(h, event)
    async with h.db() as session:
        await formation.apply_plan(
            session, h.jobs, run, request, plan, [], FormationPlanner.FALLBACK, "fake-1", h.now
        )
    return bool(plan.reserve)


async def test_apply_enqueues_rank_reserve(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=3)
    for _ in range(4):
        await enroll(h, event, await h.user())
    assert await apply_fallback(h, event) is True
    jobs = [job for job in await h.pending_jobs("rank_reserve") if job["status"] == "todo"]
    assert [job["args"] for job in jobs] == [{"event_id": str(event.id)}]

    full = await make_hackathon(h, team_size_min=3, team_size_max=3)
    for _ in range(3):
        await enroll(h, full, await h.user())
    assert await apply_fallback(h, full) is False
    jobs = [job for job in await h.pending_jobs("rank_reserve") if job["status"] == "todo"]
    assert [job["args"] for job in jobs] == [{"event_id": str(event.id)}]
