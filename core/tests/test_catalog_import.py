import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode, EventKind, EventOrigin, EventStatus
from hmm_contracts.imports import EventImport
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
