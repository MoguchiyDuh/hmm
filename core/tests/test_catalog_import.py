import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    AuditAction,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    JoinedVia,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_contracts.imports import EventImport
from hmm_contracts.notifications import AlertKind, NotificationType
from hmm_core import models
from hmm_core.catalog import service as catalog
from pydantic import JsonValue
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

IMPORT_URL = "/api/v1/internal/events/import"
FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "imports"
FIXTURE_NAMES = ("conference", "contest", "course", "hackathon", "meetup", "workshop")


def import_item(
    h: Harness,
    *,
    source_id: str = "s1",
    title: str = "Harbor Build Weekend",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    start = starts_at or h.now + timedelta(days=30)
    item: dict[str, Any] = {
        "source": {
            "source_id": source_id,
            "urls": [f"https://events.example.org/{source_id}"],
            "fetched_at": h.now.isoformat(),
        },
        "kind": "hackathon",
        "title": title,
        "starts_at": start.isoformat(),
        "ends_at": (ends_at or start + timedelta(days=2)).isoformat(),
        "registration_deadline": (h.now + timedelta(days=6)).isoformat(),
        "timezone": "UTC",
        "areas": [
            {"key": "ml_ai", "importance": "primary"},
            {"key": "backend", "importance": "required"},
            {"key": "frontend", "importance": "required"},
        ],
        "team_size_min": 3,
        "team_size_max": 6,
        "confidence": 0.95,
        "extractor_version": "test/1.0.0",
    }
    item.update(overrides)
    return item


async def post_import(h: Harness, *items: JsonValue) -> httpx.Response:
    async with h.parser() as client:
        return await client.post(IMPORT_URL, json={"items": list(items)})


async def import_results(h: Harness, *items: JsonValue) -> list[dict[str, Any]]:
    response = await post_import(h, *items)
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def count(h: Harness, model: Any, *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def existing_event(h: Harness, starts_at: datetime, ends_at: datetime) -> UUID:
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title="Py Days 2027!",
                starts_at=starts_at,
                ends_at=ends_at,
                timezone="UTC",
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        return event.id


def test_normalize_title() -> None:
    assert catalog.normalize_title("Harbor Build Weekend 2026!") == "harbor build weekend"
    assert catalog.normalize_title("harbor-build-weekend") == "harbor build weekend"


async def test_import_creates_published_event(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    results = await import_results(h, import_item(h))
    assert results[0]["result"] == "created"
    assert results[0]["review_reasons"] == []
    assert results[0]["source_id"] == "s1"
    event_id = UUID(results[0]["event_id"])
    async with h.db() as session:
        event = await catalog.load_event(session, event_id)
        assert event.status == "published"
        assert event.origin == "import"
        assert event.formation_enabled is True
        sources = list(
            await session.scalars(
                select(models.EventSource).where(models.EventSource.event_id == event_id)
            )
        )
    assert await count(h, models.EventArea, models.EventArea.event_id == event_id) == 3
    assert len(sources) == 1
    item = EventImport.model_validate(import_item(h))
    assert sources[0].payload_hash == catalog.payload_hash(item)
    assert await count(h, models.EventFormation, models.EventFormation.event_id == event_id) == 1
    jobs = await h.pending_jobs("compute_lead_fits")
    assert [job["args"] for job in jobs] == [{"event_id": str(event_id)}]


async def test_import_contract_fixtures(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    items = [json.loads((FIXTURES / f"{name}.json").read_text()) for name in FIXTURE_NAMES]
    results = await import_results(h, *items)
    assert [result["result"] for result in results] == [
        "created",
        "draft",
        "created",
        "created",
        "created",
        "created",
    ]
    assert [result["index"] for result in results] == list(range(6))
    assert results[1]["review_reasons"] == ["unmapped_areas"]
    async with h.db() as session:
        contest = await catalog.load_event(session, UUID(results[1]["event_id"]))
        assert contest.unmapped_areas == ["Reverse Engineering"]
        alerts = list(await session.scalars(select(models.AdminAlert)))
    assert [(alert.type, alert.subject_id) for alert in alerts] == [("import_review", contest.id)]
    assert await count(h, models.Event, models.Event.status == "published") == 5


async def test_import_invalid_siblings(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    broken = import_item(h, source_id="s2")
    del broken["starts_at"]
    results = await import_results(
        h, import_item(h, source_id="s1"), broken, import_item(h, source_id="s3", title="Other")
    )
    assert [result["result"] for result in results] == ["created", "rejected", "created"]
    assert results[1]["source_id"] == "s2"
    assert results[1]["errors"][0]["loc"][0] == "starts_at"
    assert_problem(await post_import(h), 422, ErrorCode.VALIDATION_ERROR)
    many = [import_item(h, source_id=f"m{index}") for index in range(51)]
    assert_problem(await post_import(h, *many), 422, ErrorCode.VALIDATION_ERROR)


async def test_import_rolls_back_failed_item(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    results = await import_results(
        h,
        import_item(h, source_id="s1", title="First"),
        import_item(h, source_id="s2", title="x" * 201),
        import_item(h, source_id="s3", title="Third"),
    )
    assert [result["result"] for result in results] == ["created", "rejected", "created"]
    assert results[1]["errors"] == [{"loc": ["item"], "msg": "Import failed"}]
    assert await count(h, models.Event) == 2
    assert await count(h, models.EventSource) == 2


async def test_import_matching_precedence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    first = import_item(h, source_id="s1")
    created = await import_results(h, first)
    assert created[0]["result"] == "created"
    event_id = created[0]["event_id"]
    hinted = import_item(h, source_id="s2", match_hint={"event_id": event_id})
    matched = await import_results(h, hinted)
    assert matched[0]["result"] == "unchanged"
    assert matched[0]["event_id"] == event_id
    assert await count(h, models.Event) == 1
    async with h.db() as session:
        rows = await session.execute(
            select(models.EventSource.source_id, models.EventSource.event_id).order_by(
                models.EventSource.source_id
            )
        )
        assert [(source_id, str(event)) for source_id, event in rows] == [
            ("s1", event_id),
            ("s2", event_id),
        ]
        event = await catalog.load_event(session, UUID(event_id))
        updated_at = event.updated_at
        source = await session.scalar(
            select(models.EventSource).where(models.EventSource.source_id == "s1")
        )
        assert source is not None
        first_seen, last_seen, digest = (
            source.first_seen_at,
            source.last_seen_at,
            source.payload_hash,
        )
    h.advance(timedelta(hours=1))
    bumped = {**first, "source": {**first["source"]}}
    bumped["source"]["fetched_at"] = h.now.isoformat()
    again = await import_results(h, bumped)
    assert again[0]["result"] == "unchanged"
    async with h.db() as session:
        source = await session.scalar(
            select(models.EventSource).where(models.EventSource.source_id == "s1")
        )
        assert source is not None
        assert source.last_seen_at == h.now
        assert source.last_seen_at > last_seen
        assert source.first_seen_at == first_seen
        assert source.payload_hash == digest
        event = await catalog.load_event(session, UUID(event_id))
        assert event.updated_at == updated_at
    assert await count(h, models.EventRevision) == 0
    assert await count(h, models.EventSource) == 2
    assert await count(h, models.EventArea) == 3
    unknown = import_item(h, source_id="s3", title="Fresh", match_hint={"event_id": str(uuid4())})
    fresh = await import_results(h, unknown)
    assert fresh[0]["result"] == "created"
    assert fresh[0]["event_id"] != event_id


async def test_import_duplicate_heuristic(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    existing_id = await existing_event(h, h.now + timedelta(days=20), h.now + timedelta(days=21))
    duplicate = await import_results(
        h, import_item(h, source_id="s1", title="py days", starts_at=h.now + timedelta(days=22))
    )
    assert duplicate[0]["result"] == "draft"
    assert duplicate[0]["review_reasons"] == ["possible_duplicate"]
    assert duplicate[0]["event_id"] != str(existing_id)
    assert await count(h, models.Event) == 2
    assert await count(h, models.AdminAlert, models.AdminAlert.type == "import_review") == 1
    later = await import_results(
        h,
        import_item(
            h,
            source_id="s2",
            title="py days",
            starts_at=h.now + timedelta(days=30),
            ends_at=h.now + timedelta(days=31),
        ),
    )
    assert later[0]["result"] == "created"
    assert later[0]["review_reasons"] == []


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(timedelta(days=3), "draft"), (timedelta(days=3, seconds=1), "created")],
)
async def test_import_duplicate_window_boundary(
    harness: Harness, offset: timedelta, expected: str
) -> None:
    h = harness
    await h.seed_areas()
    ends_at = h.now + timedelta(days=21)
    await existing_event(h, h.now + timedelta(days=20), ends_at)
    results = await import_results(h, import_item(h, title="Py Days", starts_at=ends_at + offset))
    assert results[0]["result"] == expected


async def test_import_confidence_and_areas(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    async with h.db() as session:
        area = await session.get(models.Area, areas["design"].id)
        assert area is not None
        area.archived_at = h.now
    low = await import_results(h, import_item(h, source_id="s1", title="Low", confidence=0.79))
    assert low[0]["result"] == "draft"
    assert low[0]["review_reasons"] == ["low_confidence"]
    unknown = await import_results(
        h,
        import_item(
            h, source_id="s2", title="Unknown", areas=[{"key": "alchemy", "importance": "primary"}]
        ),
    )
    assert unknown[0]["result"] == "draft"
    assert unknown[0]["review_reasons"] == ["unmapped_areas"]
    archived = await import_results(
        h,
        import_item(
            h,
            source_id="s3",
            title="Archived area",
            areas=[
                {"key": "design", "importance": "primary"},
                {"key": "backend", "importance": "nice"},
            ],
        ),
    )
    assert archived[0]["result"] == "draft"
    assert archived[0]["review_reasons"] == ["unmapped_areas"]
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(archived[0]["event_id"]))
        assert event.unmapped_areas == ["design"]
    everything = await import_results(
        h,
        import_item(
            h,
            source_id="s4",
            title="Low 2026",
            confidence=0.5,
            areas=[{"key": "alchemy", "importance": "primary"}],
        ),
    )
    assert everything[0]["result"] == "draft"
    assert everything[0]["review_reasons"] == [
        "low_confidence",
        "possible_duplicate",
        "unmapped_areas",
    ]


async def test_import_archived_match_rejected(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h, source_id="s1"))
    event_id = UUID(created[0]["event_id"])
    async with h.db() as session:
        event = await catalog.load_event(session, event_id, for_update=True)
        event.status = "archived"
    async with h.db() as session:
        before = await catalog.load_event(session, event_id)
        updated_at = before.updated_at
    results = await import_results(h, import_item(h, source_id="s1", title="Changed title"))
    assert results[0]["result"] == "rejected"
    assert results[0]["errors"] == [{"loc": ["status"], "msg": "Event is archived"}]
    async with h.db() as session:
        event = await catalog.load_event(session, event_id)
        assert event.title == "Harbor Build Weekend"
        assert event.status == "archived"
        assert event.updated_at == updated_at
    assert await count(h, models.EventRevision) == 0


async def test_import_requires_parser_token(harness: Harness) -> None:
    h = harness
    body = {"items": [import_item(h)]}
    async with h.anonymous() as client:
        response = await client.post(IMPORT_URL, json=body)
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    async with h.bot() as client:
        response = await client.post(IMPORT_URL, json=body)
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    async with h._client(headers={"Authorization": "Bearer wrong"}) as client:
        response = await client.post(IMPORT_URL, json=body)
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    member = await h.user()
    async with await h.client(member) as client:
        response = await client.post(IMPORT_URL, json=body)
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    assert await count(h, models.Event) == 0


async def admin_patch(h: Harness, event_id: str, body: dict[str, JsonValue]) -> None:
    admin = await h.admin()
    async with await h.client(admin) as client:
        response = await client.patch(f"/api/v1/admin/events/{event_id}", json=body)
    assert response.status_code == 200, response.text


async def revisions(h: Harness, event_id: str) -> list[models.EventRevision]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.EventRevision)
                .where(models.EventRevision.event_id == UUID(event_id))
                .order_by(models.EventRevision.created_at, models.EventRevision.id)
            )
        )


async def lock_rows(h: Harness, event_id: str) -> list[tuple[str, UUID, datetime]]:
    async with h.db() as session:
        rows = await session.execute(
            select(
                models.EventFieldLock.field,
                models.EventFieldLock.locked_by,
                models.EventFieldLock.locked_at,
            )
            .where(models.EventFieldLock.event_id == UUID(event_id))
            .order_by(models.EventFieldLock.field)
        )
        return [(field, locked_by, locked_at) for field, locked_by, locked_at in rows]


async def add_team(
    h: Harness, event_id: str, members: list[models.User], status: TeamStatus
) -> UUID:
    async with h.db() as session:
        team = models.Project(
            event_id=UUID(event_id),
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
        session.add_all(
            models.ProjectMember(
                project_id=team.id,
                event_id=UUID(event_id),
                user_id=member.id,
                role=MemberRole.MEMBER,
                state=MemberState.CONFIRMED,
                trust=Trust.SELF,
                joined_via=JoinedVia.ADMIN,
                is_team_row=True,
                joined_at=h.now,
            )
            for member in members
        )
        return team.id


async def add_going(h: Harness, event_id: str, user: models.User) -> None:
    async with h.db() as session:
        session.add(
            models.EventAttendance(
                event_id=UUID(event_id),
                user_id=user.id,
                status=AttendanceStatus.GOING,
                role=AttendanceRole.ATTENDEE,
                proof_links=[],
                trust=Trust.SELF,
                status_at=h.now,
            )
        )


async def test_import_updates_event(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h, summary="First summary"))
    event_id = created[0]["event_id"]
    results = await import_results(
        h, import_item(h, title="Renamed weekend", summary="Second summary", confidence=0.85)
    )
    assert results[0]["result"] == "updated"
    assert results[0]["event_id"] == event_id
    assert results[0]["ignored_fields"] == []
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.title == "Renamed weekend"
        assert event.summary == "Second summary"
        assert str(event.confidence) == "0.85"
    rows = await revisions(h, event_id)
    assert len(rows) == 1
    assert rows[0].origin == "import"
    assert rows[0].actor_id is None
    assert rows[0].ignored_fields == []
    assert rows[0].diff == {
        "title": {"before": "Harbor Build Weekend", "after": "Renamed weekend"},
        "summary": {"before": "First summary", "after": "Second summary"},
        "confidence": {"before": "0.95", "after": "0.85"},
    }
    assert await count(h, models.EventSource) == 1
    assert await count(h, models.Event) == 1


async def test_import_respects_locks(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h, summary="First summary"))
    event_id = created[0]["event_id"]
    await admin_patch(h, event_id, {"title": "Admin title"})
    locks = await lock_rows(h, event_id)
    assert [lock[0] for lock in locks] == ["title"]
    results = await import_results(h, import_item(h, title="Admin title", summary="New summary"))
    assert results[0]["result"] == "updated"
    rows = await revisions(h, event_id)
    assert rows[-1].diff == {"summary": {"before": "First summary", "after": "New summary"}}
    results = await import_results(h, import_item(h, title="Parser title", summary="Final"))
    assert results[0]["result"] == "updated"
    assert results[0]["ignored_fields"] == ["title"]
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.title == "Admin title"
        assert event.summary == "Final"
    rows = await revisions(h, event_id)
    assert rows[-1].origin == "import"
    assert rows[-1].ignored_fields == ["title"]
    assert rows[-1].diff == {"summary": {"before": "New summary", "after": "Final"}}
    total = len(rows)
    results = await import_results(h, import_item(h, title="Admin title", summary="Final"))
    assert results[0]["result"] == "unchanged"
    assert len(await revisions(h, event_id)) == total
    assert await lock_rows(h, event_id) == locks


async def test_import_match_hint_updates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h, source_id="s1"))
    event_id = created[0]["event_id"]
    results = await import_results(
        h, import_item(h, source_id="s2", title="Hinted", match_hint={"event_id": event_id})
    )
    assert results[0]["result"] == "updated"
    assert results[0]["event_id"] == event_id
    rows = await revisions(h, event_id)
    assert [row.diff for row in rows] == [
        {"title": {"before": "Harbor Build Weekend", "after": "Hinted"}}
    ]
    assert await count(h, models.EventSource, models.EventSource.event_id == UUID(event_id)) == 2


async def test_import_replaces_areas(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h))
    event_id = created[0]["event_id"]
    async with h.engine.begin() as connection:
        await connection.exec_driver_sql("DELETE FROM procrastinate.procrastinate_jobs")
    new_areas = [
        {"key": "backend", "importance": "primary", "headcount": 2},
        {"key": "design", "importance": "nice"},
    ]
    results = await import_results(h, import_item(h, areas=new_areas))
    assert results[0]["result"] == "updated"
    async with h.db() as session:
        current = await catalog.current_areas(session, UUID(event_id))
    assert current == [("backend", "primary", 2), ("design", "nice", None)]
    rows = await revisions(h, event_id)
    assert rows[-1].diff == {
        "areas": {
            "before": [
                ["backend", "required", None],
                ["frontend", "required", None],
                ["ml_ai", "primary", None],
            ],
            "after": [["backend", "primary", 2], ["design", "nice", None]],
        }
    }
    assert len(await h.pending_jobs("compute_lead_fits")) == 1
    await admin_patch(h, event_id, {"areas": [{"key": "data", "importance": "primary"}]})
    assert "areas" in [lock[0] for lock in await lock_rows(h, event_id)]
    async with h.engine.begin() as connection:
        await connection.exec_driver_sql("DELETE FROM procrastinate.procrastinate_jobs")
    results = await import_results(
        h, import_item(h, areas=[{"key": "security", "importance": "primary"}])
    )
    assert results[0]["ignored_fields"] == ["areas"]
    async with h.db() as session:
        current = await catalog.current_areas(session, UUID(event_id))
    assert current == [("data", "primary", None)]
    assert await h.pending_jobs("compute_lead_fits") == []


async def test_import_unmapped_areas_reason(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h, confidence=0.5))
    event_id = created[0]["event_id"]
    assert created[0]["review_reasons"] == ["low_confidence"]
    areas = [*import_item(h)["areas"], {"key": "alchemy", "importance": "nice"}]
    results = await import_results(h, import_item(h, confidence=0.5, areas=areas))
    assert results[0]["result"] == "updated"
    assert results[0]["review_reasons"] == ["low_confidence", "unmapped_areas"]
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.unmapped_areas == ["alchemy"]
        assert event.review_reasons == ["low_confidence", "unmapped_areas"]
    rows = await revisions(h, event_id)
    assert rows[-1].diff == {"unmapped_areas": {"before": [], "after": ["alchemy"]}}
    results = await import_results(h, import_item(h, confidence=0.5))
    assert results[0]["result"] == "updated"
    assert results[0]["review_reasons"] == ["low_confidence"]
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.unmapped_areas == []
        assert event.review_reasons == ["low_confidence"]


async def test_import_change_notifications(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    starts_at = h.now + timedelta(days=30)
    ends_at = h.now + timedelta(days=33)
    created = await import_results(h, import_item(h, starts_at=starts_at, ends_at=ends_at))
    event_id = created[0]["event_id"]
    attendee = await h.user()
    member = await h.user()
    await add_going(h, event_id, attendee)
    await add_team(h, event_id, [member], TeamStatus.FORMING)
    async with h.db() as session:
        formation_at = await session.scalar(
            select(models.EventFormation.formation_at).where(
                models.EventFormation.event_id == UUID(event_id)
            )
        )
    moved = starts_at + timedelta(days=1)
    results = await import_results(h, import_item(h, starts_at=moved, ends_at=ends_at))
    assert results[0]["result"] == "updated"
    async with h.db() as session:
        notifications = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == NotificationType.EVENT_CHANGED
                )
            )
        )
        stored_formation_at = await session.scalar(
            select(models.EventFormation.formation_at).where(
                models.EventFormation.event_id == UUID(event_id)
            )
        )
    assert sorted(row.user_id for row in notifications) == sorted([attendee.id, member.id])
    for row in notifications:
        assert isinstance(row.payload, dict)
        changes = row.payload["changes"]
        assert isinstance(changes, list)
        assert isinstance(changes[0], dict)
        assert changes[0]["field"] == "starts_at"
    assert stored_formation_at == formation_at
    results = await import_results(
        h, import_item(h, title="Renamed", starts_at=moved, ends_at=ends_at)
    )
    assert results[0]["result"] == "updated"
    assert await count(h, models.Notification) == 2


async def test_import_team_size_conflict(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h))
    event_id = created[0]["event_id"]
    members = [await h.user() for _ in range(5)]
    await add_team(h, event_id, members, TeamStatus.FORMING)
    results = await import_results(h, import_item(h, team_size_max=4))
    assert results[0]["result"] == "updated"
    async with h.db() as session:
        alerts = list(
            await session.scalars(
                select(models.AdminAlert).where(
                    models.AdminAlert.type == AlertKind.TEAM_SIZE_CONFLICT
                )
            )
        )
    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert alerts[0].subject_id == UUID(event_id)
    results = await import_results(h, import_item(h, team_size_min=2, team_size_max=6))
    assert results[0]["result"] == "updated"
    assert (
        await count(h, models.AdminAlert, models.AdminAlert.type == AlertKind.TEAM_SIZE_CONFLICT)
        == 1
    )
    assert await count(h, models.ProjectMember, models.ProjectMember.left_at.is_(None)) == 5


async def test_import_parser_cancellation(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h))
    event_id = created[0]["event_id"]
    attendee = await h.user()
    await add_going(h, event_id, attendee)
    team_id = await add_team(h, event_id, [], TeamStatus.FORMING)
    results = await import_results(h, import_item(h, status="cancelled"))
    assert results[0]["result"] == "updated"
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.status == "cancelled"
        attendance = await session.scalar(
            select(models.EventAttendance.status).where(
                models.EventAttendance.event_id == UUID(event_id)
            )
        )
        assert attendance == "cancelled"
        team = await session.get(models.Project, team_id)
        assert team is not None
        assert team.team_status == "cancelled"
        notifications = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == NotificationType.EVENT_CANCELLED
                )
            )
        )
        audits = list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == AuditAction.EVENT_CANCEL)
            )
        )
    assert [row.user_id for row in notifications] == [attendee.id]
    assert len(audits) == 1
    assert audits[0].actor_kind == "service"
    assert audits[0].actor_id is None
    assert audits[0].reason == "Cancelled by the parser"
    assert await revisions(h, event_id) == []
    again = await import_results(h, import_item(h, status="cancelled"))
    assert again[0]["result"] == "unchanged"
    assert await count(h, models.AuditLog, models.AuditLog.action == AuditAction.EVENT_CANCEL) == 1
    assert (
        await count(
            h, models.Notification, models.Notification.type == NotificationType.EVENT_CANCELLED
        )
        == 1
    )
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id), for_update=True)
        event.status = "archived"
    archived = await import_results(h, import_item(h, status="cancelled"))
    assert archived[0]["result"] == "rejected"


async def test_import_identical_payload_unchanged(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    created = await import_results(h, import_item(h))
    event_id = created[0]["event_id"]
    await add_going(h, event_id, await h.user())
    await admin_patch(h, event_id, {"title": "Admin title"})
    payload = import_item(h, title="Admin title")
    assert (await import_results(h, payload))[0]["result"] == "unchanged"
    locks = await lock_rows(h, event_id)
    total = len(await revisions(h, event_id))
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        updated_at = event.updated_at
    h.advance(timedelta(hours=1))
    bumped = {**payload, "source": {**payload["source"], "fetched_at": h.now.isoformat()}}
    results = await import_results(h, bumped)
    assert results[0]["result"] == "unchanged"
    assert len(await revisions(h, event_id)) == total
    assert await count(h, models.Notification) == 0
    assert await lock_rows(h, event_id) == locks
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.updated_at == updated_at


async def test_import_locked_only_difference_unchanged(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    base = import_item(h)
    created = await import_results(h, base)
    event_id = created[0]["event_id"]
    await admin_patch(h, event_id, {"title": "Admin title"})
    locks = await lock_rows(h, event_id)
    total = len(await revisions(h, event_id))
    async with h.db() as session:
        updated_at = (await catalog.load_event(session, UUID(event_id))).updated_at
    for title in ("Parser title", "Parser title again"):
        h.advance(timedelta(hours=1))
        results = await import_results(h, {**base, "title": title})
        assert results[0]["result"] == "unchanged"
        assert results[0]["ignored_fields"] == ["title"]
    assert len(await revisions(h, event_id)) == total
    assert await lock_rows(h, event_id) == locks
    assert await count(h, models.Notification) == 0
    async with h.db() as session:
        event = await catalog.load_event(session, UUID(event_id))
        assert event.title == "Admin title"
        assert event.updated_at == updated_at


async def test_import_offset_datetimes_unchanged(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    starts_at = h.now + timedelta(days=30)
    ends_at = starts_at + timedelta(days=2)
    deadline = h.now + timedelta(days=6)
    created = await import_results(h, import_item(h, starts_at=starts_at, ends_at=ends_at))
    event_id = created[0]["event_id"]
    plus_two = timezone(timedelta(hours=2))
    shifted = import_item(
        h,
        starts_at=starts_at.astimezone(plus_two),
        ends_at=ends_at.astimezone(plus_two),
        registration_deadline=deadline.astimezone(plus_two).isoformat(),
    )
    assert shifted["starts_at"].endswith("+02:00")
    results = await import_results(h, shifted)
    assert results[0]["result"] == "unchanged"
    assert results[0]["ignored_fields"] == []
    assert await revisions(h, event_id) == []
