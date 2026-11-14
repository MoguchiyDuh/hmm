from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import EventArea
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventPhase,
    EventStatus,
    FormationPhase,
    JoinedVia,
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
from hmm_core.formation import service as formation
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import RuntimeSettings
from pydantic import JsonValue
from sqlalchemy import select
from test_api_skeleton import assert_problem


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


def event_body(h: Harness) -> dict[str, JsonValue]:
    return {
        "kind": "hackathon",
        "title": "Py Days 2027!",
        "starts_at": (h.now + timedelta(days=30)).isoformat(),
        "ends_at": (h.now + timedelta(days=32)).isoformat(),
        "timezone": "UTC",
    }


async def test_admin_create_event_detail(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    areas = await h.seed_areas(("backend",))
    payload = {
        **event_body(h),
        "areas": [{"key": "backend", "importance": "primary", "headcount": 2}],
        "summary": "A Python hackathon",
        "description_md": "Build together",
        "organizer": "Python community",
        "website_url": "https://example.com/event",
        "registration_url": "https://example.com/register",
        "format": "hybrid",
        "location": {"city": "Berlin"},
        "language": ["en"],
        "eligibility": "Everyone",
        "themes": ["Python"],
        "prize": {"amount": 1000, "currency": "EUR"},
        "difficulty": 6.25,
        "difficulty_rationale": "Challenging",
        "reason": "Community event",
    }
    async with await h.client(admin) as client:
        response = await client.post("/api/v1/admin/events", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["origin"] == "manual"
    assert body["status"] == "published"
    assert body["slug"] == "py-days-2027"
    assert body["created_by"] == str(admin.id)
    assert body["phase"] == "registration_open"
    assert body["participation_phase"] == "open"
    assert body["registration_deadline"] is None
    assert body["areas"] == [
        {"key": "backend", "name": areas["backend"].name, "importance": "primary", "headcount": 2}
    ]
    assert body["attendance_counts"] == {"going": 0, "attended": 0}
    assert body["my_attendance"] is None
    assert body["sources"] == []
    assert body["members_going"] == []
    assert body["my_lead_fit"] is None
    assert body["formation"]["target"] == 5
    assert body["formation"]["viable"] == 4
    assert body["formation"]["core_max"] == 3
    assert body["formation"]["allowed_actions"] == []
    assert "cancel" in body["allowed_actions"]
    for field in (
        "summary",
        "description_md",
        "organizer",
        "website_url",
        "registration_url",
        "format",
        "language",
        "eligibility",
        "themes",
        "difficulty_rationale",
    ):
        assert body[field] == payload[field]
    assert body["location"] == {"country": None, "city": "Berlin", "address": None}
    assert body["prize"] == {"amount": 1000.0, "currency": "EUR", "description": None}
    assert body["difficulty"] == 6.3
    jobs = await h.pending_jobs("compute_lead_fits")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"event_id": body["id"]}
    async with h.db() as session:
        audits = (await session.scalars(select(models.AuditLog))).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.action == AuditAction.EVENT_CREATE
        assert audit.entity_type == AuditEntityType.EVENT
        assert audit.entity_id == body["id"]
        assert audit.before is None
        assert audit.after == {
            "slug": body["slug"],
            "kind": "hackathon",
            "title": payload["title"],
            "starts_at": payload["starts_at"],
            "ends_at": payload["ends_at"],
            "formation_enabled": True,
        }
        assert audit.reason == payload["reason"]


async def test_admin_create_event_errors(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with await h.client(admin) as client:
        created = await client.post("/api/v1/admin/events", json=event_body(h))
        assert created.status_code == 201, created.text
        for changes, status, code in (
            ({"formation_enabled": True, "team_size_max": 1}, 409, ErrorCode.INVALID_STATE),
            (
                {"areas": [{"key": "unknown", "importance": "primary"}]},
                422,
                ErrorCode.VALIDATION_ERROR,
            ),
            ({"slug": created.json()["slug"]}, 422, ErrorCode.VALIDATION_ERROR),
            ({"ends_at": h.now.isoformat()}, 422, ErrorCode.VALIDATION_ERROR),
        ):
            assert_problem(
                await client.post("/api/v1/admin/events", json={**event_body(h), **changes}),
                status,
                code,
            )
    assert len(await h.pending_jobs("compute_lead_fits")) == 1
    async with h.db() as session:
        assert len((await session.scalars(select(models.Event))).all()) == 1
        assert len((await session.scalars(select(models.AuditLog))).all()) == 1


async def test_create_event_requires_admin(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with await h.client(member) as client:
        assert_problem(
            await client.post("/api/v1/admin/events", json=event_body(h)),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with h.anonymous() as client:
        assert_problem(
            await client.post("/api/v1/admin/events", json=event_body(h)),
            401,
            ErrorCode.UNAUTHENTICATED,
        )


async def test_member_event_detail_actions(harness: Harness) -> None:
    h = harness
    creator = await h.admin()
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=creator.id,
            now=h.now,
        )
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/events/{event.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["my_attendance"] is None
        assert body["allowed_actions"] == ["going", "enroll"]
        async with h.db() as session:
            session.add(
                models.Enrollment(
                    event_id=event.id,
                    user_id=member.id,
                    status=EnrollmentStatus.ACTIVE,
                    source=EnrollmentSource.SELF,
                    status_at=h.now,
                )
            )
        response = await client.get(f"/api/v1/events/{event.id}")
        assert response.status_code == 200, response.text
        assert response.json()["allowed_actions"] == ["going", "withdraw", "request_join"]
        assert response.json()["my_lead_fit"] is None
        async with h.db() as session:
            session.add(
                models.LeadFit(
                    event_id=event.id,
                    user_id=member.id,
                    fit=Decimal("0.90"),
                    reason="Experienced lead",
                    algo_version="test",
                    computed_at=h.now,
                )
            )
        response = await client.get(f"/api/v1/events/{event.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["allowed_actions"] == ["going", "withdraw", "claim_lead", "request_join"]
        assert body["my_lead_fit"]["eligible"] is True
        assert body["my_lead_fit"]["fit"] == 0.9
        assert body["formation"]["my_state"]["lead_fit"] == body["my_lead_fit"]
    h.advance(timedelta(days=30))
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/events/{event.id}")
        assert response.status_code == 200, response.text
        assert response.json()["allowed_actions"] == ["going", "attended"]


@pytest.mark.parametrize("status", [EventStatus.DRAFT, EventStatus.ARCHIVED])
async def test_event_detail_hidden_statuses(harness: Harness, status: EventStatus) -> None:
    h = harness
    member = await h.user()
    admin = await h.admin()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=member.id,
            now=h.now,
        )
    async with await h.client(member) as client:
        assert_problem(await client.get(f"/api/v1/events/{event.id}"), 404, ErrorCode.NOT_FOUND)
    async with await h.client(admin) as client:
        response = await client.get(f"/api/v1/events/{event.id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status


async def test_cancelled_event_detail_requires_involvement(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.CANCELLED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(member) as client:
        assert_problem(await client.get(f"/api/v1/events/{event.id}"), 404, ErrorCode.NOT_FOUND)
        async with h.db() as session:
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=member.id,
                    status=AttendanceStatus.CANCELLED,
                    role=AttendanceRole.ATTENDEE,
                    proof_links=[],
                    trust=Trust.SELF,
                    status_at=h.now,
                )
            )
        response = await client.get(f"/api/v1/events/{event.id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["allowed_actions"] == []
    assert response.json()["my_attendance"]["allowed_actions"] == []


async def test_event_detail_attendance(harness: Harness) -> None:
    h = harness
    first = await h.user()
    second = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        for index, user in enumerate((first, second)):
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=user.id,
                    status=AttendanceStatus.GOING,
                    role=AttendanceRole.ATTENDEE,
                    proof_links=["https://example.com/proof"],
                    note="See you there",
                    trust=Trust.SELF,
                    status_at=h.now - timedelta(hours=index),
                )
            )
    async with await h.client(first) as client:
        response = await client.get(f"/api/v1/events/{event.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attendance_counts"] == {"going": 2, "attended": 0}
    assert [user["id"] for user in body["members_going"]] == [str(second.id), str(first.id)]
    assert body["my_attendance"]["allowed_actions"] == ["edit", "remove"]
    assert body["my_attendance"]["user_id"] == str(first.id)
    assert body["my_attendance"]["event_id"] == str(event.id)
    assert body["my_attendance"]["event"]["title"] == event.title
    assert body["my_attendance"]["user"]["display_name"] == first.display_name
    assert body["my_attendance"]["status"] == "going"
    assert body["my_attendance"]["proof_links"] == ["https://example.com/proof"]
    assert body["my_attendance"]["note"] == "See you there"
    assert "going" not in body["allowed_actions"]


async def test_event_detail_unknown_event(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with await h.client(member) as client:
        assert_problem(await client.get(f"/api/v1/events/{uuid4()}"), 404, ErrorCode.NOT_FOUND)


@pytest.mark.parametrize("kind", [EventKind.CONFERENCE, EventKind.HACKATHON])
async def test_event_detail_formation_block(harness: Harness, kind: EventKind) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), kind=kind),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/events/{event.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    if kind == EventKind.CONFERENCE:
        assert body["formation"] is None
        assert body["participation_phase"] is None
        assert body["my_lead_fit"] is None
    else:
        assert body["participation_phase"] == "open"
        assert body["formation"]["counts"] == {
            "enrollments": 0,
            "active": 0,
            "placed": 0,
            "reserve": 0,
            "cores": 0,
            "core_members": 0,
            "teams": 0,
            "volunteers": 0,
        }
        assert body["formation"]["projection"] == {
            "teams": 0,
            "new_teams": 0,
            "placed": 0,
            "reserve": 0,
            "undersized_cores": 0,
            "team_sizes": [],
            "volunteers": 0,
        }
        assert body["formation"]["my_state"] == {
            "enrollment": None,
            "team": None,
            "membership": None,
            "lead_fit": None,
        }
        assert body["formation"]["allowed_actions"] == []


@pytest.fixture
async def catalog_events(harness: Harness) -> dict[str, models.Event]:
    h = harness
    await h.seed_areas(("backend",))
    payloads = {
        "hackathon": replace(
            event_input(h),
            title="Python 100%_\\ Festival",
            starts_at=h.now + timedelta(days=10),
            ends_at=h.now + timedelta(days=12),
            format=EventFormat.ONLINE,
            areas=(catalog.EventAreaInput(key="backend", importance=AreaImportance.PRIMARY),),
        ),
        "conference": replace(
            event_input(h),
            kind=EventKind.CONFERENCE,
            title="Systems conference",
            summary="A systems gathering",
            starts_at=h.now + timedelta(days=20),
            ends_at=h.now + timedelta(days=22),
            registration_deadline=h.now - timedelta(days=1),
            format=EventFormat.HYBRID,
        ),
        "contest": replace(
            event_input(h),
            kind=EventKind.CONTEST,
            title="Code contest",
            registration_opens_at=h.now + timedelta(days=5),
            format=EventFormat.OFFLINE,
        ),
        "cancelled": replace(event_input(h), title="Cancelled event"),
        "draft": replace(event_input(h), title="Draft event"),
        "archived": replace(event_input(h), title="Archived event"),
    }
    events: dict[str, models.Event] = {}
    async with h.db() as session:
        for key, payload in payloads.items():
            events[key] = await catalog.create_event(
                session,
                payload,
                origin={"conference": EventOrigin.MEMBER, "contest": EventOrigin.IMPORT}.get(
                    key, EventOrigin.MANUAL
                ),
                status={
                    "cancelled": EventStatus.CANCELLED,
                    "draft": EventStatus.DRAFT,
                    "archived": EventStatus.ARCHIVED,
                }.get(key, EventStatus.PUBLISHED),
                created_by=None,
                now=h.now,
            )
        schedule = await session.get(models.EventFormation, events["contest"].id)
        assert schedule is not None
        schedule.phase = FormationPhase.REVIEW
    return events


@pytest.mark.parametrize("admin", [False, True])
async def test_catalog_list_visibility_and_summaries(
    harness: Harness, catalog_events: dict[str, models.Event], admin: bool
) -> None:
    h = harness
    actor = await h.user(admin=admin)
    async with await h.client(actor) as client:
        response = await client.get("/api/v1/events")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    assert [item["id"] for item in body["items"]] == [
        str(catalog_events[key].id) for key in ("contest", "conference", "hackathon")
    ]
    for item in body["items"]:
        assert item["status"] == "published"
        assert item["attendance_counts"] == {"going": 0, "attended": 0}
        assert item["my_attendance"] is None
        assert "going" in item["allowed_actions"]
        assert "attended" not in item["allowed_actions"]
    contest, conference, hackathon = body["items"]
    assert contest["areas"] == []
    assert contest["participation_phase"] == "review"
    assert conference["areas"] == []
    assert conference["participation_phase"] is None
    assert hackathon["areas"] == [
        {"key": "backend", "name": "Backend", "importance": "primary", "headcount": None}
    ]
    assert hackathon["participation_phase"] == "open"


async def test_catalog_list_involved_cancelled_event(
    harness: Harness, catalog_events: dict[str, models.Event]
) -> None:
    h = harness
    actor = await h.user()
    async with h.db() as session:
        for key in ("cancelled", "archived", "draft"):
            session.add(
                models.EventAttendance(
                    event_id=catalog_events[key].id,
                    user_id=actor.id,
                    status=AttendanceStatus.GOING,
                    role=AttendanceRole.ATTENDEE,
                    proof_links=[],
                    trust=Trust.SELF,
                    status_at=h.now,
                )
            )
    async with await h.client(actor) as client:
        response = await client.get("/api/v1/events")
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {
        str(catalog_events[key].id) for key in ("contest", "conference", "hackathon", "cancelled")
    }
    cancelled = items[str(catalog_events["cancelled"].id)]
    assert cancelled["status"] == "cancelled"
    assert cancelled["attendance_counts"] == {"going": 1, "attended": 0}
    assert cancelled["my_attendance"]["status"] == "going"
    assert cancelled["allowed_actions"] == []


async def test_catalog_list_filters(
    harness: Harness, catalog_events: dict[str, models.Event]
) -> None:
    h = harness
    actor = await h.user()
    async with await h.client(actor) as client:
        for query, expected in (
            ({"kind": "conference"}, "conference"),
            ({"formation": "false"}, "conference"),
            ({"format": "online"}, "hackathon"),
            ({"origin": "import"}, "contest"),
            ({"area": "backend"}, "hackathon"),
            ({"q": "pyThOn"}, "hackathon"),
            ({"q": "GATHERING"}, "conference"),
            ({"q": "%"}, "hackathon"),
            ({"q": "_"}, "hackathon"),
            ({"q": "\\"}, "hackathon"),
            ({"participation_phase": "open"}, "hackathon"),
            ({"participation_phase": "review"}, "contest"),
            ({"phase": "registration_open"}, "hackathon"),
            ({"phase": "registration_closed"}, "conference"),
            ({"phase": "upcoming"}, "contest"),
        ):
            response = await client.get("/api/v1/events", params=query)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [
                str(catalog_events[expected].id)
            ], query
        for query in (
            {"participation_phase": "locked"},
            {"phase": "ended"},
            {"q": "does not exist"},
        ):
            response = await client.get("/api/v1/events", params=query)
            assert response.status_code == 200, response.text
            assert response.json() == {"items": [], "next_cursor": None}
        response = await client.get("/api/v1/events", params={"q": ""})
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 3
        async with h.db() as session:
            area = await session.scalar(select(models.Area).where(models.Area.key == "backend"))
            assert area is not None
            area.archived_at = h.now
        response = await client.get("/api/v1/events", params={"area": "backend"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [
            str(catalog_events["hackathon"].id)
        ]
        h.advance(timedelta(days=10))
        response = await client.get("/api/v1/events", params={"phase": "ongoing"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [
            str(catalog_events["hackathon"].id)
        ]
        h.advance(timedelta(days=2))
        response = await client.get("/api/v1/events", params={"phase": "ended"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [
            str(catalog_events["hackathon"].id)
        ]


async def test_catalog_list_date_overlap(harness: Harness) -> None:
    h = harness
    actor = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(
                event_input(h),
                starts_at=h.now + timedelta(days=10),
                ends_at=h.now + timedelta(days=12),
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(actor) as client:
        for params in (
            {"from": (h.now + timedelta(days=11)).isoformat()},
            {"from": event.ends_at.isoformat()},
            {"to": event.starts_at.isoformat()},
            {
                "from": (h.now + timedelta(days=11)).isoformat(),
                "to": (h.now + timedelta(days=11)).isoformat(),
            },
        ):
            response = await client.get("/api/v1/events", params=params)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [str(event.id)]
        for params in (
            {"from": (h.now + timedelta(days=13)).isoformat()},
            {"to": (h.now + timedelta(days=9)).isoformat()},
        ):
            response = await client.get("/api/v1/events", params=params)
            assert response.status_code == 200, response.text
            assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("attendance_status", [AttendanceStatus.GOING, AttendanceStatus.ATTENDED])
async def test_catalog_list_mine(
    harness: Harness,
    catalog_events: dict[str, models.Event],
    attendance_status: AttendanceStatus,
) -> None:
    h = harness
    actor = await h.user()
    async with h.db() as session:
        unrelated = await catalog.create_event(
            session,
            replace(event_input(h), title="Unrelated event"),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=actor.id,
            now=h.now,
        )
        session.add(
            models.EventAttendance(
                event_id=catalog_events["hackathon"].id,
                user_id=actor.id,
                status=attendance_status,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )
        session.add(
            models.Enrollment(
                event_id=catalog_events["contest"].id,
                user_id=actor.id,
                status=EnrollmentStatus.ACTIVE,
                source=EnrollmentSource.SELF,
                status_at=h.now,
            )
        )
        project = models.Project(
            event_id=catalog_events["conference"].id,
            name="Event team",
            description_md="Team description",
            goal="Build",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.ACTIVE,
            team_status=TeamStatus.FORMING,
            origin=ProjectOrigin.ADMIN,
        )
        session.add(project)
        await session.flush()
        membership = models.ProjectMember(
            project_id=project.id,
            event_id=catalog_events["conference"].id,
            user_id=actor.id,
            role=MemberRole.MEMBER,
            state=MemberState.CONFIRMED,
            trust=Trust.SELF,
            joined_via=JoinedVia.ADMIN,
            is_team_row=True,
            joined_at=h.now,
        )
        session.add(membership)
        await session.flush()
        membership_id = membership.id
    expected = {str(catalog_events[key].id) for key in ("hackathon", "contest", "conference")}
    async with await h.client(actor) as client:
        response = await client.get("/api/v1/events", params={"mine": "true"})
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["items"]} == expected
        for params in ({"mine": "false"}, {}):
            response = await client.get("/api/v1/events", params=params)
            assert response.status_code == 200, response.text
            assert {item["id"] for item in response.json()["items"]} == expected | {
                str(unrelated.id)
            }
        async with h.db() as session:
            stored = await session.get(models.ProjectMember, membership_id)
            assert stored is not None
            stored.left_at = h.now
        response = await client.get("/api/v1/events", params={"mine": "true"})
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["items"]} == expected - {
            str(catalog_events["conference"].id)
        }


@pytest.mark.parametrize("tied_dates", [False, True])
async def test_catalog_list_cursor(harness: Harness, tied_dates: bool) -> None:
    h = harness
    actor = await h.user()
    events: list[models.Event] = []
    async with h.db() as session:
        for index in range(3):
            events.append(
                await catalog.create_event(
                    session,
                    replace(
                        event_input(h),
                        starts_at=h.now + timedelta(days=10 if tied_dates else 10 + index),
                    ),
                    origin=EventOrigin.MANUAL,
                    status=EventStatus.PUBLISHED,
                    created_by=None,
                    now=h.now,
                )
            )
    ordered = sorted(events, key=lambda event: (event.starts_at, event.id), reverse=True)
    async with await h.client(actor) as client:
        first = await client.get("/api/v1/events", params={"limit": 2})
        assert first.status_code == 200, first.text
        first_page = first.json()
        assert [item["id"] for item in first_page["items"]] == [
            str(event.id) for event in ordered[:2]
        ]
        assert first_page["next_cursor"] is not None
        second = await client.get(
            "/api/v1/events", params={"limit": 2, "cursor": first_page["next_cursor"]}
        )
        assert second.status_code == 200, second.text
        second_page = second.json()
        assert [item["id"] for item in second_page["items"]] == [str(ordered[2].id)]
        assert second_page["next_cursor"] is None
        assert {item["id"] for item in first_page["items"]}.isdisjoint(
            item["id"] for item in second_page["items"]
        )
        assert_problem(
            await client.get("/api/v1/events", params={"cursor": "garbage"}),
            400,
            ErrorCode.BAD_REQUEST,
        )


async def test_catalog_list_requires_onboarded_member(harness: Harness) -> None:
    h = harness
    async with h.anonymous() as client:
        assert_problem(await client.get("/api/v1/events"), 401, ErrorCode.UNAUTHENTICATED)
    actor = await h.user(onboarded=False)
    async with await h.client(actor) as client:
        assert_problem(await client.get("/api/v1/events"), 403, ErrorCode.ONBOARDING_REQUIRED)
