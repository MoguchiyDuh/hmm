from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
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
    InviteKind,
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    MemberRole,
    MemberState,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    SwapRequestStatus,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import AlertKind, NotificationChange, NotificationType
from hmm_core import models
from hmm_core.catalog import jobs as catalog_jobs
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JOBS, REGISTRY, JobName
from hmm_core.platform.settings import RuntimeSettings
from pydantic import JsonValue
from sqlalchemy import select, update
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


async def catalog_team(
    h: Harness, event: models.Event, member: models.User | None, status: TeamStatus
) -> models.Project:
    async with h.db() as session:
        team = models.Project(
            event_id=event.id,
            name=f"{status.value} team",
            description_md="Team description",
            goal="Build together",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.ACTIVE,
            team_status=status.value,
            origin=ProjectOrigin.ADMIN,
        )
        session.add(team)
        await session.flush()
        if member is not None:
            session.add(
                models.ProjectMember(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=member.id,
                    role=MemberRole.MEMBER,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.ADMIN,
                    is_team_row=True,
                    joined_at=h.now,
                )
            )
        return team


async def test_admin_cancel_event_cascade_and_idempotency(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    attendee = await h.user()
    enrolled = await h.user()
    member = await h.user()
    historical = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=admin.id,
            now=h.now,
        )
    forming = await catalog_team(h, event, member, TeamStatus.FORMING)
    finished = await catalog_team(h, event, None, TeamStatus.FINISHED)
    async with h.db() as session:
        for user, status in (
            (attendee, AttendanceStatus.GOING),
            (historical, AttendanceStatus.ATTENDED),
        ):
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=user.id,
                    status=status,
                    role=AttendanceRole.ATTENDEE,
                    proof_links=[],
                    trust=Trust.SELF,
                    status_at=h.now - timedelta(days=1),
                )
            )
        for user, status in (
            (attendee, EnrollmentStatus.ACTIVE),
            (enrolled, EnrollmentStatus.RESERVE),
            (member, EnrollmentStatus.PLACED),
            (historical, EnrollmentStatus.WITHDRAWN),
        ):
            session.add(
                models.Enrollment(
                    event_id=event.id,
                    user_id=user.id,
                    status=status,
                    source=EnrollmentSource.SELF,
                    status_at=h.now - timedelta(days=1),
                )
            )
        invites = [
            models.Invite(
                project_id=forming.id,
                event_id=event.id,
                user_id=attendee.id,
                kind=InviteKind.CORE,
                status=status,
                expires_at=h.now + timedelta(days=1),
            )
            for status in (InviteStatus.PENDING, InviteStatus.ACCEPTED)
        ]
        requests = [
            models.JoinRequest(
                project_id=forming.id,
                event_id=event.id,
                user_id=enrolled.id,
                status=status,
            )
            for status in (JoinRequestStatus.PENDING, JoinRequestStatus.ACCEPTED)
        ]
        swaps = [
            models.SwapRequest(
                event_id=event.id,
                from_team_id=forming.id,
                from_user_id=member.id,
                to_team_id=finished.id,
                to_user_id=attendee.id,
                requested_by=member.id,
                status=status,
            )
            for status in (SwapRequestStatus.PENDING, SwapRequestStatus.DECLINED)
        ]
        merges = [
            models.MergeProposal(
                event_id=event.id,
                absorbed_team_id=forming.id,
                target_team_id=finished.id,
                proposed_by=MergeProposedBy.SYSTEM,
                status=status,
            )
            for status in (MergeProposalStatus.PENDING, MergeProposalStatus.DECLINED)
        ]
        offers = [
            models.LeadOffer(
                project_id=forming.id,
                event_id=event.id,
                user_id=enrolled.id,
                source=LeadOfferSource.SUCCESSION,
                reason="Lead needed",
                status=status,
                expires_at=h.now + timedelta(days=1),
            )
            for status in (LeadOfferStatus.PENDING, LeadOfferStatus.ACCEPTED)
        ]
        session.add_all([*invites, *requests, *swaps, *merges, *offers])
        await session.flush()
        assert await catalog.event_recipients(session, event.id) == sorted(
            [attendee.id, enrolled.id, member.id], key=str
        )
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/events/{event.id}/cancel", json={"reason": "Organizer postponed"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["participation_phase"] == "closed"
        async with h.db() as session:
            attendances = {
                row.user_id: row for row in await session.scalars(select(models.EventAttendance))
            }
            assert attendances[attendee.id].status == AttendanceStatus.CANCELLED
            assert attendances[attendee.id].status_at == h.now
            assert attendances[historical.id].status == AttendanceStatus.ATTENDED
            assert attendances[historical.id].status_at == h.now - timedelta(days=1)
            enrollments = {
                row.user_id: row for row in await session.scalars(select(models.Enrollment))
            }
            for user in (attendee, enrolled, member):
                assert enrollments[user.id].status == EnrollmentStatus.EXPIRED
                assert enrollments[user.id].status_at == h.now
            assert enrollments[historical.id].status == EnrollmentStatus.WITHDRAWN
            assert enrollments[historical.id].status_at == h.now - timedelta(days=1)
            teams = {row.id: row for row in await session.scalars(select(models.Project))}
            assert teams[forming.id].team_status == TeamStatus.CANCELLED
            assert teams[forming.id].status == ProjectStatus.CANCELLED
            assert teams[finished.id].team_status == TeamStatus.FINISHED
            assert teams[finished.id].status == ProjectStatus.ACTIVE
            schedule = await session.get(models.EventFormation, event.id)
            assert schedule is not None and schedule.phase == FormationPhase.CLOSED
            for model, pending, terminal, terminal_status in (
                (models.Invite, invites[0], invites[1], InviteStatus.ACCEPTED),
                (models.JoinRequest, requests[0], requests[1], JoinRequestStatus.ACCEPTED),
                (models.SwapRequest, swaps[0], swaps[1], SwapRequestStatus.DECLINED),
                (models.MergeProposal, merges[0], merges[1], MergeProposalStatus.DECLINED),
                (models.LeadOffer, offers[0], offers[1], LeadOfferStatus.ACCEPTED),
            ):
                rows = {row.id: row for row in await session.scalars(select(model))}
                assert rows[pending.id].status == "closed"
                assert rows[terminal.id].status == terminal_status
            invite = await session.get(models.Invite, invites[0].id)
            assert invite is not None and invite.closed_reason == "event_cancelled"
            notifications = list(await session.scalars(select(models.Notification)))
            cancelled = [
                row for row in notifications if row.type == NotificationType.EVENT_CANCELLED
            ]
            assert {row.user_id for row in cancelled} == {attendee.id, enrolled.id, member.id}
            assert len(cancelled) == 3
            for notification in cancelled:
                assert isinstance(notification.payload, dict)
                assert notification.payload["reason"] == "Organizer postponed"
                assert notification.payload["link"] == f"/events/{event.slug}"
                assert (
                    notification.dedupe_key == f"event_cancelled:{event.id}:{notification.user_id}"
                )
            alerts = list(await session.scalars(select(models.AdminAlert)))
            assert len(alerts) == 1
            assert alerts[0].type == AlertKind.EVENT_CANCELLED_WITH_TEAMS
            assert alerts[0].severity == "warning"
            assert alerts[0].subject_type == "event"
            assert alerts[0].subject_id == event.id
            assert isinstance(alerts[0].payload, dict)
            assert alerts[0].payload["message"] == "1 teams were cancelled with the event"
            assert alerts[0].payload["link"] == f"/admin/events/{event.id}"
            audits = list(await session.scalars(select(models.AuditLog)))
            assert len(audits) == 1
            assert audits[0].action == AuditAction.EVENT_CANCEL
            assert audits[0].entity_type == AuditEntityType.EVENT
            assert audits[0].entity_id == str(event.id)
            assert audits[0].actor_id == admin.id
            assert audits[0].actor_kind == ActorKind.ADMIN
            assert audits[0].reason == "Organizer postponed"
            assert audits[0].before == {"status": "published"}
            assert audits[0].after == {"status": "cancelled"}
            updated_at = (await catalog.load_event(session, event.id)).updated_at
            notification_ids = {row.id for row in notifications}
            alert_ids = {row.id for row in alerts}
            audit_ids = {row.id for row in audits}
        h.advance(timedelta(seconds=1))
        for reason in ("Organizer postponed", "Different reason"):
            response = await client.post(
                f"/api/v1/admin/events/{event.id}/cancel", json={"reason": reason}
            )
            assert response.status_code == 200, response.text
        async with h.db() as session:
            assert set(await session.scalars(select(models.Notification.id))) == notification_ids
            assert set(await session.scalars(select(models.AdminAlert.id))) == alert_ids
            assert set(await session.scalars(select(models.AuditLog.id))) == audit_ids
            assert (await catalog.load_event(session, event.id)).updated_at == updated_at


async def test_cancel_event_without_teams_has_no_alert(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=admin.id,
            now=h.now,
        )
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/events/{event.id}/cancel", json={"reason": "Cancelled by organizer"}
        )
    assert response.status_code == 200, response.text
    async with h.db() as session:
        assert list(await session.scalars(select(models.AdminAlert))) == []
        assert list(await session.scalars(select(models.Notification))) == []


async def test_cancel_event_errors(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    async with h.db() as session:
        archived = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.ARCHIVED,
            created_by=None,
            now=h.now,
        )
        published = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(admin) as client:
        assert_problem(
            await client.post(
                f"/api/v1/admin/events/{archived.id}/cancel", json={"reason": "Cancel"}
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await client.post(f"/api/v1/admin/events/{uuid4()}/cancel", json={"reason": "Cancel"}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await client.post(f"/api/v1/admin/events/{published.id}/cancel", json={}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    async with await h.client(member) as client:
        assert_problem(
            await client.post(
                f"/api/v1/admin/events/{published.id}/cancel", json={"reason": "Cancel"}
            ),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with h.db() as session:
        assert (await catalog.load_event(session, archived.id)).status == EventStatus.ARCHIVED
        assert (await catalog.load_event(session, published.id)).status == EventStatus.PUBLISHED
        assert list(await session.scalars(select(models.AuditLog))) == []


async def test_archive_old_events(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    events: dict[str, models.Event] = {}
    async with h.db() as session:
        for key, status, days in (
            ("published", EventStatus.PUBLISHED, 200),
            ("cancelled", EventStatus.CANCELLED, 200),
            ("draft", EventStatus.DRAFT, 200),
            ("recent", EventStatus.PUBLISHED, 100),
            ("boundary", EventStatus.PUBLISHED, 180),
            ("archived", EventStatus.ARCHIVED, 200),
        ):
            events[key] = await catalog.create_event(
                session,
                replace(
                    event_input(h),
                    title=key,
                    formation_enabled=False,
                    starts_at=h.now - timedelta(days=days + 2),
                    ends_at=h.now - timedelta(days=days),
                ),
                origin=EventOrigin.MANUAL,
                status=status,
                created_by=None,
                now=h.now,
            )
        await session.execute(
            update(models.Event)
            .where(models.Event.id == events["archived"].id)
            .values(updated_at=h.now - timedelta(days=10))
        )
    assert JOBS[JobName.ARCHIVE_OLD_EVENTS].cron == "30 1 * * *"
    assert JobName.ARCHIVE_OLD_EVENTS in REGISTRY
    await catalog_jobs.archive_old_events(h.runtime)
    async with h.db() as session:
        for key, event in events.items():
            stored = await catalog.load_event(session, event.id)
            expected = (
                EventStatus.PUBLISHED if key in ("recent", "boundary") else EventStatus.ARCHIVED
            )
            assert stored.status == expected
        archived = await catalog.load_event(session, events["archived"].id)
        assert archived.updated_at == h.now - timedelta(days=10)
        first = {
            row.id: (row.status, row.updated_at)
            for row in await session.scalars(select(models.Event))
        }
        assert list(await session.scalars(select(models.AuditLog))) == []
    await catalog_jobs.archive_old_events(h.runtime)
    async with h.db() as session:
        assert {
            row.id: (row.status, row.updated_at)
            for row in await session.scalars(select(models.Event))
        } == first
        session.add(models.Setting(key="event_archive_after_days", value=90, updated_by=admin.id))
    await catalog_jobs.archive_old_events(h.runtime)
    async with h.db() as session:
        assert (
            await catalog.load_event(session, events["recent"].id)
        ).status == EventStatus.ARCHIVED
        assert (
            await catalog.load_event(session, events["boundary"].id)
        ).status == EventStatus.ARCHIVED
        assert (await catalog.load_event(session, events["archived"].id)).updated_at == first[
            events["archived"].id
        ][1]


async def test_notify_event_changed(harness: Harness) -> None:
    h = harness
    attendee = await h.user()
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        empty = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=attendee.id,
                status=AttendanceStatus.GOING,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )
    await catalog_team(h, event, member, TeamStatus.FORMING)
    changes = [
        NotificationChange(
            field="starts_at",
            previous=event.starts_at.isoformat(),
            current=(event.starts_at + timedelta(days=1)).isoformat(),
        )
    ]
    async with h.db() as session:
        await catalog.notify_event_changed(session, event, changes, h.now)
        await catalog.notify_event_changed(session, event, changes, h.now)
        await catalog.notify_event_changed(session, event, [], h.now)
        await catalog.notify_event_changed(session, empty, changes, h.now)
    async with h.db() as session:
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert {row.user_id for row in notifications} == {attendee.id, member.id}
        for row in notifications:
            assert row.type == NotificationType.EVENT_CHANGED
            assert isinstance(row.payload, dict)
            assert row.payload["link"] == f"/events/{event.slug}"
            assert row.payload["event_id"] == str(event.id)
            assert row.payload["changes"] == [changes[0].model_dump(mode="json")]
            assert row.payload["timezone"] == event.timezone


@pytest.fixture
async def editable_event(harness: Harness) -> models.Event:
    h = harness
    await h.seed_areas(("backend", "design"))
    async with h.db() as session:
        return await catalog.create_event(
            session,
            replace(
                event_input(h),
                team_size_min=2,
                team_size_max=5,
                areas=(
                    catalog.EventAreaInput(
                        key="backend", importance=AreaImportance.PRIMARY, headcount=2
                    ),
                ),
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def test_admin_patch_title_locks_revisions_and_audit(
    harness: Harness, editable_event: models.Event
) -> None:
    h = harness
    first = await h.admin()
    second = await h.admin()
    event = editable_event
    async with await h.client(first) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}",
            json={"title": "New title", "reason": "Correct title"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "New title"
        locks = await client.get(f"/api/v1/admin/events/{event.id}/locks")
        assert locks.status_code == 200, locks.text
        original_lock = locks.json()[0]
        assert len(locks.json()) == 1
        assert original_lock["field"] == "title"
        assert original_lock["locked_by"] == str(first.id)
        assert original_lock["allowed_actions"] == ["unlock"]
        revisions = await client.get(f"/api/v1/admin/events/{event.id}/revisions")
        assert revisions.status_code == 200, revisions.text
        assert len(revisions.json()) == 1
        revision = revisions.json()[0]
        assert revision["origin"] == "admin"
        assert revision["actor_id"] == str(first.id)
        assert revision["ignored_fields"] == []
        assert revision["diff"] == {"title": {"before": event.title, "after": "New title"}}
    async with h.db() as session:
        audits = list(await session.scalars(select(models.AuditLog)))
        assert len(audits) == 1
        assert audits[0].action == AuditAction.EVENT_EDIT
        assert audits[0].actor_id == first.id
        assert audits[0].actor_kind == ActorKind.ADMIN
        assert audits[0].before == {"title": event.title}
        assert audits[0].after == {"title": "New title"}
        assert audits[0].reason == "Correct title"
    h.advance(timedelta(hours=1))
    async with await h.client(second) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"title": "Latest title"}
        )
        assert response.status_code == 200, response.text
        response = await client.get(f"/api/v1/admin/events/{event.id}/locks")
        assert response.status_code == 200, response.text
        assert response.json() == [original_lock]
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"themes": ["Python"]}
        )
        assert response.status_code == 200, response.text
        response = await client.get(f"/api/v1/admin/events/{event.id}/locks")
        assert response.status_code == 200, response.text
        assert [lock["field"] for lock in response.json()] == ["themes", "title"]
        response = await client.get(f"/api/v1/admin/events/{event.id}/revisions")
        assert response.status_code == 200, response.text
        revisions = response.json()
        assert [row["diff"] for row in revisions] == [
            {"themes": {"before": [], "after": ["Python"]}},
            {"title": {"before": "New title", "after": "Latest title"}},
            {"title": {"before": event.title, "after": "New title"}},
        ]
        assert all(row["ignored_fields"] == [] for row in revisions)
        assert [row["id"] for row in revisions] == [
            row["id"]
            for row in sorted(
                revisions, key=lambda row: (row["created_at"], row["id"]), reverse=True
            )
        ]


async def test_admin_patch_areas(harness: Harness, editable_event: models.Event) -> None:
    h = harness
    admin = await h.admin()
    event = editable_event
    payload = {"areas": [{"key": "design", "importance": "required", "headcount": 3}]}
    async with await h.client(admin) as client:
        response = await client.patch(f"/api/v1/admin/events/{event.id}", json=payload)
        assert response.status_code == 200, response.text
        assert [area["key"] for area in response.json()["areas"]] == ["design"]
        async with h.db() as session:
            rows = (
                await session.execute(
                    select(models.Area.key, models.EventArea.importance, models.EventArea.headcount)
                    .join(models.EventArea, models.EventArea.area_id == models.Area.id)
                    .where(models.EventArea.event_id == event.id)
                )
            ).all()
            assert rows == [("design", "required", 3)]
            locks = list(await session.scalars(select(models.EventFieldLock)))
            assert len(locks) == 1 and locks[0].field == "areas"
            revisions = list(await session.scalars(select(models.EventRevision)))
            assert len(revisions) == 1
            assert revisions[0].diff == {
                "areas": {
                    "before": [{"key": "backend", "importance": "primary", "headcount": 2}],
                    "after": payload["areas"],
                }
            }
            revision_id = revisions[0].id
        jobs = await h.pending_jobs("compute_lead_fits")
        assert len(jobs) == 1 and jobs[0]["args"] == {"event_id": str(event.id)}
        assert_problem(
            await client.patch(
                f"/api/v1/admin/events/{event.id}",
                json={
                    "summary": "Should roll back",
                    "areas": [{"key": "unknown", "importance": "primary"}],
                },
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        response = await client.patch(f"/api/v1/admin/events/{event.id}", json=payload)
        assert response.status_code == 200, response.text
        async with h.db() as session:
            assert list(await session.scalars(select(models.EventRevision.id))) == [revision_id]
            assert (await catalog.load_event(session, event.id)).summary is None
            assert [
                lock.field for lock in await session.scalars(select(models.EventFieldLock))
            ] == ["areas"]
        response = await client.patch(f"/api/v1/admin/events/{event.id}", json={"areas": []})
        assert response.status_code == 200, response.text
        assert response.json()["areas"] == []


async def test_unlock_event_field(harness: Harness, editable_event: models.Event) -> None:
    h = harness
    locker = await h.admin()
    admin = await h.admin()
    member = await h.user()
    event = editable_event
    async with await h.client(locker) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"title": "Locked title"}
        )
        assert response.status_code == 200, response.text
    async with await h.client(admin) as client:
        response = await client.delete(
            f"/api/v1/admin/events/{event.id}/locks/title", params={"reason": "fix"}
        )
        assert response.status_code == 204, response.text
        assert response.content == b""
        response = await client.get(f"/api/v1/admin/events/{event.id}/locks")
        assert response.status_code == 200 and response.json() == []
        for field in ("title", "unknown"):
            assert_problem(
                await client.delete(f"/api/v1/admin/events/{event.id}/locks/{field}"),
                404,
                ErrorCode.NOT_FOUND,
            )
    async with await h.client(member) as client:
        assert_problem(
            await client.delete(f"/api/v1/admin/events/{event.id}/locks/title"),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with h.db() as session:
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == AuditAction.EVENT_UNLOCK_FIELD
                )
            )
        )
        assert len(audits) == 1
        assert audits[0].actor_id == admin.id
        assert audits[0].before == {
            "field": "title",
            "locked_by": str(locker.id),
            "locked_at": h.now.isoformat(),
        }
        assert audits[0].after is None
        assert audits[0].reason == "fix"


async def test_patch_event_merged_validation(
    harness: Harness, editable_event: models.Event
) -> None:
    h = harness
    admin = await h.admin()
    event = editable_event
    async with await h.client(admin) as client:
        for payload in (
            {"ends_at": (event.starts_at - timedelta(seconds=1)).isoformat()},
            {"team_size_min": 6},
            {"team_size_min": 21},
            {"team_size_max": 21},
        ):
            assert_problem(
                await client.patch(f"/api/v1/admin/events/{event.id}", json=payload),
                422,
                ErrorCode.VALIDATION_ERROR,
            )
        for field in (
            "slug",
            "kind",
            "formation_enabled",
            "title",
            "starts_at",
            "ends_at",
            "timezone",
            "language",
            "themes",
        ):
            problem = assert_problem(
                await client.patch(f"/api/v1/admin/events/{event.id}", json={field: None}),
                422,
                ErrorCode.VALIDATION_ERROR,
            )
            assert problem.detail == f"{field} cannot be null"
        async with h.db() as session:
            assert list(await session.scalars(select(models.EventRevision))) == []
            assert list(await session.scalars(select(models.EventFieldLock))) == []
            assert list(await session.scalars(select(models.AuditLog))) == []
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}",
            json={
                "starts_at": (event.starts_at + timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text


async def test_patch_event_idempotency(harness: Harness, editable_event: models.Event) -> None:
    h = harness
    admin = await h.admin()
    event = editable_event
    async with await h.client(admin) as client:
        for payload in ({"title": event.title}, {}, {"status": "published"}, {"areas": None}):
            response = await client.patch(f"/api/v1/admin/events/{event.id}", json=payload)
            assert response.status_code == 200, response.text
    async with h.db() as session:
        assert list(await session.scalars(select(models.EventRevision))) == []
        assert list(await session.scalars(select(models.EventFieldLock))) == []
        assert list(await session.scalars(select(models.AuditLog))) == []
        assert list(await session.scalars(select(models.Notification))) == []
        assert (await catalog.load_event(session, event.id)).updated_at == event.updated_at
    assert await h.pending_jobs("compute_lead_fits") == []


@pytest.mark.parametrize("blocker", ["enrollment", "team", "single_member"])
async def test_patch_event_formation_guards(harness: Harness, blocker: str) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(
                event_input(h),
                formation_enabled=False,
                team_size_max=1 if blocker == "single_member" else 5,
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        if blocker == "enrollment":
            session.add(
                models.Enrollment(
                    event_id=event.id,
                    user_id=member.id,
                    status=EnrollmentStatus.ACTIVE,
                    source=EnrollmentSource.SELF,
                    status_at=h.now,
                )
            )
    if blocker == "team":
        await catalog_team(h, event, None, TeamStatus.FORMING)
    async with await h.client(admin) as client:
        assert_problem(
            await client.patch(
                f"/api/v1/admin/events/{event.id}", json={"formation_enabled": True}
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
    async with h.db() as session:
        assert (await catalog.load_event(session, event.id)).formation_enabled is False
        assert list(await session.scalars(select(models.EventFieldLock))) == []
        assert list(await session.scalars(select(models.EventRevision))) == []


async def test_patch_event_enable_and_disable_formation(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), formation_enabled=False),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(admin) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"formation_enabled": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["formation"]["phase"] == "open"
        async with h.db() as session:
            schedule = await session.get(models.EventFormation, event.id)
            assert schedule is not None
            original = (schedule.phase, schedule.formation_at, schedule.review_ends_at)
            lock = await session.get(models.EventFieldLock, (event.id, "formation_enabled"))
            assert lock is not None and lock.locked_by == admin.id
        jobs = await h.pending_jobs("compute_lead_fits")
        assert len(jobs) == 1 and jobs[0]["args"] == {"event_id": str(event.id)}
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"formation_enabled": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["formation"] is None
    async with h.db() as session:
        schedule = await session.get(models.EventFormation, event.id)
        assert schedule is not None
        assert (schedule.phase, schedule.formation_at, schedule.review_ends_at) == original
    assert len(await h.pending_jobs("compute_lead_fits")) == 1


@pytest.mark.parametrize("team_status", [TeamStatus.DISBANDED, TeamStatus.CANCELLED])
async def test_patch_event_terminal_rows_allow_formation(
    harness: Harness, team_status: TeamStatus
) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            replace(event_input(h), formation_enabled=False),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        session.add_all(
            models.Enrollment(
                event_id=event.id,
                user_id=member.id,
                status=status,
                source=EnrollmentSource.SELF,
                status_at=h.now,
            )
            for status in (
                EnrollmentStatus.WITHDRAWN,
                EnrollmentStatus.LAPSED,
                EnrollmentStatus.EXPIRED,
            )
        )
    await catalog_team(h, event, None, team_status)
    async with await h.client(admin) as client:
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"formation_enabled": True}
        )
    assert response.status_code == 200, response.text
    assert response.json()["formation"]["phase"] == "open"


async def test_patch_event_notifications_and_date_hook(
    harness: Harness, editable_event: models.Event
) -> None:
    h = harness
    event = editable_event
    admin = await h.admin()
    attendee = await h.user()
    member = await h.user()
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=attendee.id,
                status=AttendanceStatus.GOING,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )
    await catalog_team(h, event, member, TeamStatus.FORMING)
    new_start = event.starts_at + timedelta(days=1)
    with patch.object(formation, "dates_changed", wraps=formation.dates_changed) as hook:
        async with await h.client(admin) as client:
            response = await client.patch(
                f"/api/v1/admin/events/{event.id}", json={"starts_at": new_start.isoformat()}
            )
            assert response.status_code == 200, response.text
            hook.assert_awaited_once()
            assert hook.await_args is not None
            assert hook.await_args.args[3] == formation.event_dates(event)
            async with h.db() as session:
                notifications = list(await session.scalars(select(models.Notification)))
                assert len(notifications) == 2
                assert {row.user_id for row in notifications} == {attendee.id, member.id}
                for row in notifications:
                    assert row.type == NotificationType.EVENT_CHANGED
                    assert isinstance(row.payload, dict)
                    assert row.payload["changes"] == [
                        {
                            "field": "starts_at",
                            "previous": event.starts_at.isoformat(),
                            "current": new_start.isoformat(),
                        }
                    ]
            response = await client.patch(
                f"/api/v1/admin/events/{event.id}", json={"format": "online"}
            )
            assert response.status_code == 200, response.text
            async with h.db() as session:
                notifications = list(await session.scalars(select(models.Notification)))
                assert len(notifications) == 4
                formats = [
                    row
                    for row in notifications
                    if isinstance(row.payload, dict)
                    and row.payload["changes"]
                    == [{"field": "format", "previous": None, "current": "online"}]
                ]
                assert {row.user_id for row in formats} == {attendee.id, member.id}
            response = await client.patch(
                f"/api/v1/admin/events/{event.id}", json={"title": "New title"}
            )
            assert response.status_code == 200, response.text
            hook.assert_awaited_once()
    async with h.db() as session:
        assert len(list(await session.scalars(select(models.Notification)))) == 4


@pytest.mark.parametrize("reason", [None, "Organizer cancelled"])
async def test_patch_event_status_cancellation(
    harness: Harness, editable_event: models.Event, reason: str | None
) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    event = editable_event
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=event.id,
                user_id=member.id,
                status=AttendanceStatus.GOING,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )
    async with await h.client(admin) as client:
        assert_problem(
            await client.patch(f"/api/v1/admin/events/{event.id}", json={"status": "draft"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}",
            json={
                "status": "cancelled",
                "reason": reason,
                "title": "Ignore this",
                "areas": [{"key": "unknown", "importance": "primary"}],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["title"] == event.title
    assert response.json()["participation_phase"] == "closed"
    async with h.db() as session:
        attendance = await session.scalar(select(models.EventAttendance))
        assert attendance is not None and attendance.status == AttendanceStatus.CANCELLED
        audits = list(await session.scalars(select(models.AuditLog)))
        assert len(audits) == 1 and audits[0].action == AuditAction.EVENT_CANCEL
        assert audits[0].reason == (reason or "Cancelled by admin")
        assert list(await session.scalars(select(models.EventRevision))) == []
        assert list(await session.scalars(select(models.EventFieldLock))) == []


async def test_patch_event_normalization(harness: Harness, editable_event: models.Event) -> None:
    h = harness
    admin = await h.admin()
    event = editable_event
    async with h.db() as session:
        other = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
    payload = {
        "slug": "corrected-event",
        "kind": "contest",
        "location": {"city": "Berlin", "address": None},
        "prize": {"amount": 1000, "currency": "EUR", "description": None},
        "difficulty": 6.25,
        "website_url": "https://example.com/event",
        "registration_url": "https://example.com/register",
        "language": ["en"],
        "review_reasons": ["manual", "low_confidence"],
    }
    async with await h.client(admin) as client:
        response = await client.patch(f"/api/v1/admin/events/{event.id}", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["difficulty"] == 6.3
        assert body["review_reasons"] == ["manual", "low_confidence"]
        assert body["kind"] == "contest"
        response = await client.patch(f"/api/v1/admin/events/{event.id}", json=payload)
        assert response.status_code == 200, response.text
        assert_problem(
            await client.patch(f"/api/v1/admin/events/{event.id}", json={"slug": other.slug}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    async with h.db() as session:
        stored = await catalog.load_event(session, event.id)
        assert stored.location == {"city": "Berlin"}
        assert stored.prize == {"amount": 1000.0, "currency": "EUR"}
        assert stored.website_url == payload["website_url"]
        assert stored.registration_url == payload["registration_url"]
        assert stored.difficulty == Decimal("6.3")
        revisions = list(await session.scalars(select(models.EventRevision)))
        assert len(revisions) == 1
        assert isinstance(revisions[0].diff, dict)
        assert revisions[0].diff["difficulty"] == {"before": None, "after": "6.3"}
        fields = set(await session.scalars(select(models.EventFieldLock.field)))
        assert fields == payload.keys() - {"review_reasons"}
        assert "status" not in fields


async def test_patch_event_access_and_archived_edit(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            event_input(h),
            origin=EventOrigin.MANUAL,
            status=EventStatus.ARCHIVED,
            created_by=None,
            now=h.now,
        )
    async with await h.client(member) as client:
        assert_problem(
            await client.patch(f"/api/v1/admin/events/{event.id}", json={"title": "Not allowed"}),
            403,
            ErrorCode.FORBIDDEN,
        )
    async with await h.client(admin) as client:
        assert_problem(
            await client.patch(f"/api/v1/admin/events/{uuid4()}", json={"title": "Not found"}),
            404,
            ErrorCode.NOT_FOUND,
        )
        response = await client.patch(
            f"/api/v1/admin/events/{event.id}", json={"title": "Archived edit"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Archived edit"
        assert response.json()["status"] == "archived"
        for endpoint in ("locks", "revisions"):
            assert_problem(
                await client.get(f"/api/v1/admin/events/{uuid4()}/{endpoint}"),
                404,
                ErrorCode.NOT_FOUND,
            )


async def listing_project(
    h: Harness,
    event: models.Event,
    *,
    name: str,
    lead: models.User | None,
    members: tuple[models.User, ...] = (),
    team_status: TeamStatus | None = TeamStatus.FORMING,
    visibility: ProjectVisibility = ProjectVisibility.PUBLIC,
    area_keys: tuple[str, ...] = (),
    **fields: object,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=name,
            description_md="Project description",
            goal="Ship it",
            links=["https://example.com/repo"],
            visibility=visibility,
            status=ProjectStatus.ACTIVE,
            team_status=team_status.value if team_status is not None else None,
            origin=ProjectOrigin.ADMIN if team_status is not None else ProjectOrigin.STANDALONE,
            lead_user_id=lead.id if lead is not None else None,
            **fields,
        )
        session.add(project)
        await session.flush()
        areas = {
            area.key: area.id
            for area in await session.scalars(
                select(models.Area).where(models.Area.key.in_(area_keys))
            )
        }
        session.add_all(
            models.ProjectArea(project_id=project.id, area_id=areas[key]) for key in area_keys
        )
        rows = [(lead, MemberRole.LEAD)] if lead is not None else []
        rows += [(member, MemberRole.MEMBER) for member in members]
        session.add_all(
            models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=user.id,
                role=role,
                state=MemberState.CONFIRMED,
                trust=Trust.SELF,
                joined_via=JoinedVia.ADMIN,
                is_team_row=team_status is not None,
                joined_at=h.now - timedelta(hours=index) if role == MemberRole.MEMBER else h.now,
            )
            for index, (user, role) in enumerate(rows)
        )
        return project


async def published_event(h: Harness, **fields: object) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            replace(event_input(h), **fields),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


async def test_list_event_projects_full_views(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "design"))
    event = await published_event(h)
    lead, first, second, viewer = await h.user(), await h.user(), await h.user(), await h.user()
    team = await listing_project(
        h,
        event,
        name="Alpha team",
        lead=lead,
        members=(first, second),
        area_keys=("design", "backend"),
    )
    standalone = await listing_project(h, event, name="Aardvark", lead=first, team_status=None)
    async with await h.client(viewer) as client:
        response = await client.get(f"/api/v1/events/{event.id}/projects")
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [str(team.id), str(standalone.id)]
    team_view, standalone_view = items
    assert team_view["view"] == "full"
    assert [member["user_id"] for member in team_view["members"]] == [
        str(lead.id),
        str(second.id),
        str(first.id),
    ]
    assert team_view["members"][0]["role"] == "lead"
    assert team_view["members"][0]["user"]["display_name"] == lead.display_name
    assert team_view["area_keys"] == ["backend", "design"]
    assert team_view["open_seats"] is None
    assert team_view["status"] == "active"
    assert team_view["team_status"] == "forming"
    assert team_view["links"] == ["https://example.com/repo"]
    assert team_view["allowed_actions"] == []
    assert team_view["pending_invites"] is None
    assert standalone_view["view"] == "full"
    assert standalone_view["event_id"] == str(event.id)
    assert standalone_view["team_status"] is None
    assert standalone_view["open_seats"] is None


async def test_list_event_projects_limited_view(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend",))
    event = await published_event(h)
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    project = await listing_project(
        h,
        event,
        name="Hidden",
        lead=lead,
        members=(member,),
        team_status=None,
        visibility=ProjectVisibility.MEMBERS,
        area_keys=("backend",),
    )
    async with await h.client(outsider) as client:
        response = await client.get(f"/api/v1/events/{event.id}/projects")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "view": "limited",
            "id": str(project.id),
            "name": "Hidden",
            "area_keys": ["backend"],
            "allowed_actions": [],
        }
    ]
    for user in (member, lead):
        async with await h.client(user) as client:
            response = await client.get(f"/api/v1/events/{event.id}/projects")
        assert response.status_code == 200, response.text
        [item] = response.json()
        assert item["view"] == "full"
        assert {row["user_id"] for row in item["members"]} == {str(lead.id), str(member.id)}
    async with await h.client(member) as client:
        [item] = (await client.get(f"/api/v1/events/{event.id}/projects")).json()
    assert item["allowed_actions"] == ["leave"]


async def test_list_event_projects_pending_items(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend",))
    event = await published_event(h)
    lead, member, outsider = await h.user(), await h.user(), await h.user()
    invitee, requester, heir = await h.user(), await h.user(), await h.user()
    team = await listing_project(h, event, name="Team", lead=lead, members=(member, heir))
    async with h.db() as session:
        backend = await session.scalar(select(models.Area).where(models.Area.key == "backend"))
        assert backend is not None
        session.add_all(
            [
                models.Invite(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=invitee.id,
                    invited_by=lead.id,
                    kind=InviteKind.FILL,
                    status=InviteStatus.PENDING,
                    area_id=backend.id,
                    expires_at=h.now + timedelta(days=1),
                ),
                models.Invite(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=outsider.id,
                    kind=InviteKind.FILL,
                    status=InviteStatus.DECLINED,
                    expires_at=h.now + timedelta(days=1),
                ),
                models.JoinRequest(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=requester.id,
                    note="Let me in",
                    status=JoinRequestStatus.PENDING,
                ),
                models.LeadOffer(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=heir.id,
                    offered_by=lead.id,
                    source=LeadOfferSource.TRANSFER,
                    reason="Take over",
                    status=LeadOfferStatus.PENDING,
                    expires_at=h.now + timedelta(days=3),
                ),
            ]
        )
    async with await h.client(lead) as client:
        response = await client.get(f"/api/v1/events/{event.id}/projects")
    assert response.status_code == 200, response.text
    [item] = response.json()
    [invite] = item["pending_invites"]
    assert invite["user_id"] == str(invitee.id)
    assert invite["inviter"]["id"] == str(lead.id)
    assert invite["area_key"] == "backend"
    assert invite["project"] == {
        "id": str(team.id),
        "name": "Team",
        "event_id": str(event.id),
        "team_status": "forming",
    }
    [request] = item["pending_join_requests"]
    assert request["user_id"] == str(requester.id) and request["note"] == "Let me in"
    assert item["pending_lead_offer"]["user_id"] == str(heir.id)
    assert item["pending_lead_offer"]["offerer"]["id"] == str(lead.id)
    assert "transfer_lead" in item["allowed_actions"]
    for user in (member, outsider):
        async with await h.client(user) as client:
            [item] = (await client.get(f"/api/v1/events/{event.id}/projects")).json()
        assert item["view"] == "full"
        assert item["pending_invites"] is None
        assert item["pending_join_requests"] is None
        assert item["pending_lead_offer"] is None


async def test_list_event_projects_open_seats(harness: Harness) -> None:
    h = harness
    event = await published_event(h, team_size_min=2, team_size_max=5)
    lead, other_lead = await h.user(), await h.user()
    members = [await h.user() for _ in range(4)]
    open_team = await listing_project(
        h,
        event,
        name="Alpha",
        lead=lead,
        members=tuple(members[:2]),
        team_status=TeamStatus.LOCKED,
    )
    closed_team = await listing_project(
        h,
        event,
        name="Beta",
        lead=other_lead,
        members=tuple(members[2:]),
        team_status=TeamStatus.LOCKED,
        seats_closed_at=h.now,
    )
    async with await h.client(lead) as client:
        items = (await client.get(f"/api/v1/events/{event.id}/projects")).json()
    assert [item["id"] for item in items] == [str(open_team.id), str(closed_team.id)]
    assert [item["open_seats"] for item in items] == [2, 2]
    assert "close_seats" in items[0]["allowed_actions"]
    async with await h.client(other_lead) as client:
        items = (await client.get(f"/api/v1/events/{event.id}/projects")).json()
    assert "reopen_seats" in items[1]["allowed_actions"]
    assert "close_seats" not in items[1]["allowed_actions"]


async def test_list_event_projects_errors(harness: Harness) -> None:
    h = harness
    event = await published_event(h)
    member = await h.user()
    async with await h.client(member) as client:
        assert_problem(
            await client.get(f"/api/v1/events/{uuid4()}/projects"), 404, ErrorCode.NOT_FOUND
        )
        response = await client.get(f"/api/v1/events/{event.id}/projects")
        assert response.status_code == 200 and response.json() == []
    async with h.anonymous() as client:
        assert_problem(
            await client.get(f"/api/v1/events/{event.id}/projects"), 401, ErrorCode.UNAUTHENTICATED
        )
