from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import EventArea
from hmm_contracts.common import (
    AreaImportance,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventPhase,
    EventStatus,
    FormationPhase,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import RuntimeSettings
from sqlalchemy import select


def event_input(h: Harness) -> catalog.EventInput:
    return catalog.EventInput(
        kind=EventKind.HACKATHON,
        title="Py Days 2027!",
        starts_at=h.now + timedelta(days=30),
        ends_at=h.now + timedelta(days=32),
        timezone="UTC",
    )


async def test_create_published_event(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        event_id = event.id
    async with h.db() as session:
        stored = await catalog.load_event(session, event_id)
        assert stored.origin == "manual"
        assert stored.status == "published"
        assert stored.slug == "py-days-2027"
        assert stored.formation_enabled is True
        assert stored.registration_deadline is None
        schedule = await session.get(models.EventFormation, event_id)
        assert schedule is not None
        assert schedule.phase == FormationPhase.OPEN
        assert schedule.formation_at == stored.starts_at - timedelta(hours=120)
        assert schedule.review_ends_at == schedule.formation_at + timedelta(hours=48)
    assert await h.pending_jobs("compute_lead_fits") == []


async def test_short_notice_formation(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), starts_at=h.now + timedelta(days=2)),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        timing = formation.formation_timing(event, RuntimeSettings(), h.now)
        assert timing.reg_deadline == event.starts_at
        assert timing.team_deadline == h.now + timedelta(hours=47)
        assert timing.formation_at == h.now + timedelta(hours=24)
        assert timing.review_ends_at == h.now + timedelta(hours=47)
        schedule = await session.get(models.EventFormation, event.id)
        assert schedule is not None
        assert schedule.formation_at == timing.formation_at
        assert schedule.review_ends_at == timing.review_ends_at


@pytest.mark.parametrize(
    ("kind", "team_size_max"),
    [(EventKind.HACKATHON, 1), (EventKind.CONFERENCE, None)],
)
async def test_default_formation_disabled(
    harness: Harness, kind: EventKind, team_size_max: int | None
) -> None:
    h = harness
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), kind=kind, team_size_max=team_size_max),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        assert event.formation_enabled is False
        assert await formation.ensure_formation(session, event, h.now) is None
        assert await session.get(models.EventFormation, event.id) is None


async def test_explicit_formation_rejects_single_member_teams(harness: Harness) -> None:
    h = harness
    with pytest.raises(ProblemError) as error:
        async with h.db() as session:
            await catalog.create_event(
                session,
                replace(event_input(h), team_size_max=1, formation_enabled=True),
                origin=EventOrigin.MANUAL,
                status=EventStatus.PUBLISHED,
                created_by=None,
                now=h.now,
            )
    assert error.value.code == ErrorCode.INVALID_STATE


async def test_event_slugs(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        first = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        second = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        assert first.slug == "py-days-2027"
        assert second.slug == "py-days-2027-2"
    with pytest.raises(ProblemError) as error:
        async with h.db() as session:
            await catalog.create_event(
                session,
                replace(event_input(h), slug=first.slug),
                origin=EventOrigin.MANUAL,
                status=EventStatus.PUBLISHED,
                created_by=None,
                now=h.now,
            )
    assert error.value.code == ErrorCode.VALIDATION_ERROR


async def test_event_areas(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "design", "archived"))
    async with h.db() as session:
        archived = await session.get(models.Area, areas["archived"].id)
        assert archived is not None
        archived.archived_at = h.now
    backend = catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY, headcount=2)
    design = catalog.EventAreaInput(key="design", importance=AreaImportance.NICE)
    for inputs, detail in (
        ((replace(backend, key="archived"),), "Unknown area: archived"),
        ((replace(backend, key="unknown"),), "Unknown area: unknown"),
        ((backend, backend), "Duplicate area: backend"),
    ):
        with pytest.raises(ProblemError) as error:
            async with h.db() as session:
                await catalog.create_event(
                    session,
                    replace(event_input(h), areas=inputs),
                    origin=EventOrigin.MANUAL,
                    status=EventStatus.PUBLISHED,
                    created_by=None,
                    now=h.now,
                )
        assert error.value.code == ErrorCode.VALIDATION_ERROR
        assert error.value.detail == detail
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), areas=(design, backend)),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        event_id = event.id
    async with h.db() as session:
        rows = (await session.scalars(select(models.EventArea))).all()
        assert {(row.event_id, row.area_id, row.importance, row.headcount) for row in rows} == {
            (event_id, areas["backend"].id, "primary", 2),
            (event_id, areas["design"].id, "nice", None),
        }


async def test_event_phase_boundaries(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(
                event_input(h),
                registration_opens_at=h.now + timedelta(days=10),
                registration_deadline=h.now + timedelta(days=20),
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        for days, expected in (
            (9, EventPhase.UPCOMING),
            (10, EventPhase.REGISTRATION_OPEN),
            (11, EventPhase.REGISTRATION_OPEN),
            (20, EventPhase.REGISTRATION_CLOSED),
            (21, EventPhase.REGISTRATION_CLOSED),
            (30, EventPhase.ONGOING),
            (32, EventPhase.ENDED),
        ):
            assert catalog.event_phase(event, h.now + timedelta(days=days)) == expected
        event.registration_deadline = None
        assert (
            catalog.event_phase(event, h.now + timedelta(days=29)) == EventPhase.REGISTRATION_OPEN
        )
        assert catalog.event_phase(event, event.starts_at) == EventPhase.ONGOING
        event.registration_opens_at = None
        assert catalog.event_phase(event, h.now) == EventPhase.REGISTRATION_OPEN


async def test_draft_formation_created_later(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.DRAFT,
            created_by=None,
            now=h.now,
        )
        assert event.formation_enabled is True
        assert await session.get(models.EventFormation, event.id) is None
        schedule = await formation.ensure_formation(session, event, h.now)
        assert schedule is not None
        assert schedule.phase == "open"
        original = (schedule.formation_at, schedule.review_ends_at)
        event.starts_at += timedelta(days=5)
        event.ends_at += timedelta(days=5)
        existing = await formation.ensure_formation(session, event, h.now + timedelta(days=1))
        assert existing is schedule
        assert (existing.formation_at, existing.review_ends_at) == original


async def test_event_snapshot(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("design", "backend", "data"))
    creator = await h.admin()
    async with h.db() as session:
        backend = await session.get(models.Area, areas["backend"].id)
        data = await session.get(models.Area, areas["data"].id)
        assert backend is not None and data is not None
        backend.sort = 10
        data.sort = 0
        event = await catalog.create_event(
            session,
            replace(
                event_input(h),
                format=EventFormat.ONLINE,
                summary="Summary",
                description_md="Description",
                themes=("Python",),
                team_size_min=2,
                team_size_max=4,
                difficulty=Decimal("6.25"),
                areas=(
                    catalog.EventAreaInput(key="design", importance=AreaImportance.NICE),
                    catalog.EventAreaInput(
                        key="backend", importance=AreaImportance.PRIMARY, headcount=2
                    ),
                    catalog.EventAreaInput(key="data", importance=AreaImportance.REQUIRED),
                ),
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=creator.id,
            now=h.now,
        )
        snapshot = await catalog.event_snapshot(session, event.id)
        assert snapshot.id == event.id
        assert snapshot.title == event.title
        assert snapshot.timezone == event.timezone
        assert snapshot.summary == event.summary
        assert snapshot.description_md == event.description_md
        assert snapshot.themes == ["Python"]
        assert snapshot.starts_at == event.starts_at
        assert snapshot.ends_at == event.ends_at
        assert snapshot.team_size_min == 2
        assert snapshot.team_size_max == 4
        assert snapshot.created_by == creator.id
        assert snapshot.areas == [
            EventArea(area="data", importance=AreaImportance.REQUIRED, headcount=None),
            EventArea(area="backend", importance=AreaImportance.PRIMARY, headcount=2),
            EventArea(area="design", importance=AreaImportance.NICE, headcount=None),
        ]
        assert snapshot.difficulty == 6.3
        assert isinstance(snapshot.difficulty, float)
        assert snapshot.kind is EventKind.HACKATHON
        assert snapshot.origin is EventOrigin.MANUAL
        assert snapshot.format is EventFormat.ONLINE
        event.format = None
        event.difficulty = None
        snapshot = await catalog.event_snapshot(session, event.id)
        assert snapshot.format is None
        assert snapshot.difficulty is None
    with pytest.raises(ProblemError) as error:
        async with h.db() as session:
            await catalog.event_snapshot(session, uuid4())
    assert error.value.code == ErrorCode.NOT_FOUND


async def test_enqueue_lead_fits(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        published = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        draft = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.DRAFT,
            created_by=None,
            now=h.now,
        )
        disabled = await catalog.create_event(
            session,
            replace(event_input(h), formation_enabled=False),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        await catalog.enqueue_lead_fits(session, h.jobs, draft)
        await catalog.enqueue_lead_fits(session, h.jobs, disabled)
    assert await h.pending_jobs("compute_lead_fits") == []
    async with h.db() as session:
        await catalog.enqueue_lead_fits(session, h.jobs, published)
    jobs = await h.pending_jobs("compute_lead_fits")
    assert len(jobs) == 1
    assert jobs[0]["status"] == "todo"
    assert jobs[0]["args"] == {"event_id": str(published.id)}
