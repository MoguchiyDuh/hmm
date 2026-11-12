import json
from pathlib import Path
from typing import Any

import pytest
from hmm_contracts import imports
from hmm_contracts.common import Contract, EventKind, SourcePersonRole
from hmm_contracts.imports import (
    AreaListResponse,
    EventImport,
    EventImportBatch,
    EventImportResponse,
    EventImportResult,
    ImportArea,
    ParserHeartbeat,
    SourcePerson,
    TrackedEvent,
    TrackedEventsResponse,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "imports"
FIXTURE_PATHS = sorted(FIXTURES.glob("*.json"))


@pytest.fixture
def payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "hackathon.json").read_text())


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_fixture_validates_and_round_trips(path: Path) -> None:
    event = EventImport.model_validate_json(path.read_text())
    assert EventImport.model_validate_json(event.model_dump_json()) == event
    assert isinstance(event, Contract)


def test_fixtures_cover_required_kinds() -> None:
    assert len(FIXTURE_PATHS) >= 6
    kinds = {EventImport.model_validate_json(path.read_text()).kind for path in FIXTURE_PATHS}
    assert kinds == set(EventKind) - {EventKind.OTHER}


@pytest.mark.parametrize("kind", EventKind)
def test_every_event_kind(payload: dict[str, Any], kind: EventKind) -> None:
    payload["kind"] = kind
    assert EventImport.model_validate(payload).kind == kind


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ends_at", "2026-11-05T17:00:00Z"),
        ("team_size_min", 6),
        ("team_size_min", 0),
        ("team_size_max", 0),
        ("confidence", -0.01),
        ("confidence", 1.01),
        ("confidence", float("nan")),
        ("field_confidence", {"title": -0.01}),
        ("field_confidence", {"title": 1.01}),
        ("field_confidence", {"title": float("inf")}),
        ("summary", "a" * 501),
        ("description_md", "a" * 20001),
        ("website_url", "ftp://events.example.org"),
        ("registration_url", "not a URL"),
        ("status", "published"),
        ("kind", "festival"),
        ("format", "remote"),
        ("timezone", "Not/AZone"),
        ("timezone", "/etc/passwd"),
        ("language", ["eng"]),
        ("prize", {"currency": "euros"}),
        ("difficulty", {"level": 0, "rationale": "Too low"}),
        ("difficulty", {"level": 11, "rationale": "Too high"}),
        ("difficulty", {"level": 5}),
        ("areas", [{"key": "", "importance": "primary"}]),
        ("areas", [{"key": " \t", "importance": "primary"}]),
        ("areas", [{"key": "a" * 65, "importance": "primary"}]),
        ("areas", [{"key": "backend", "importance": "mandatory"}]),
        ("match_hint", {"event_id": "not-a-uuid"}),
        (
            "source",
            {
                "source_id": "bad",
                "urls": ["file:///etc/passwd"],
                "fetched_at": "2026-11-12T00:00:00Z",
            },
        ),
        ("source", {"source_id": "bad", "urls": [], "fetched_at": "2026-11-12T00:00:00"}),
        ("unexpected", True),
    ],
)
def test_invalid_event_fields(payload: dict[str, Any], field: str, value: object) -> None:
    payload[field] = value
    with pytest.raises(ValidationError):
        EventImport.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["starts_at", "ends_at", "registration_opens_at", "registration_deadline"]
)
def test_event_datetimes_require_timezone(payload: dict[str, Any], field: str) -> None:
    payload[field] = "2026-11-06T17:00:00"
    with pytest.raises(ValidationError, match="timezone_aware"):
        EventImport.model_validate(payload)


@pytest.mark.parametrize(
    "person",
    [
        {"role": "attendee"},
        {"role": "attendee", "github_login": "mira"},
        {"role": "attendee", "telegram_username": "mira"},
        {"role": "attendee", "display_name": None},
        {"role": "attendee", "display_name": ""},
        {"role": "attendee", "github_login": " ", "telegram_username": "", "display_name": "\t"},
        {"role": "winner", "display_name": "Mira Vale", "place": 0},
        {"role": "winner", "display_name": "Mira Vale", "place": -1},
        {"role": "host", "display_name": "Mira Vale"},
    ],
)
def test_invalid_source_person(person: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SourcePerson.model_validate(person)


@pytest.mark.parametrize(
    "role", [role for role in SourcePersonRole if role != SourcePersonRole.WINNER]
)
def test_only_winners_have_places(role: SourcePersonRole) -> None:
    with pytest.raises(ValidationError, match="Only a winner"):
        SourcePerson(display_name="Mira Vale", role=role, place=1)


@pytest.mark.parametrize("handles", [{}, {"github_login": "mira"}, {"telegram_username": "mira"}])
def test_person_requires_name_with_optional_handles(handles: dict[str, str]) -> None:
    person = SourcePerson.model_validate(
        {"display_name": " Mira Vale ", "role": "winner"} | handles
    )
    assert person.display_name == "Mira Vale"
    assert person.place is None


def test_nullable_facts_and_free_text_eligibility(payload: dict[str, Any]) -> None:
    for name, field in EventImport.model_fields.items():
        if not field.is_required() and field.default is None:
            payload[name] = None
    payload["eligibility"] = "Students only; ages 18-25; residency requirements apply."
    event = EventImport.model_validate(payload)
    assert event.eligibility == payload["eligibility"]
    assert event.team_size_min is None
    assert event.team_size_max is None


def test_boundaries_and_unmapped_areas(payload: dict[str, Any]) -> None:
    payload.update(
        ends_at=payload["starts_at"],
        registration_deadline="2026-11-09T00:00:00Z",
        team_size_min=21,
        team_size_max=100,
        confidence=0,
        field_confidence={"title": 1},
        summary="a" * 500,
        description_md="a" * 20000,
        difficulty={"level": 10, "rationale": "Expert challenge"},
        areas=[{"key": "new_specialty", "importance": "primary"}],
    )
    event = EventImport.model_validate(payload)
    assert event.areas[0].key == "new_specialty"
    assert event.ends_at == event.starts_at
    assert event.registration_deadline is not None
    assert event.registration_deadline > event.starts_at
    assert event.team_size_min == 21
    assert event.team_size_max == 100


@pytest.mark.parametrize("key", ["ML AI", "new-specialty", "a" * 64])
def test_unmapped_area_keys_are_free_text(key: str) -> None:
    area = ImportArea.model_validate({"key": f" {key} ", "importance": "primary"})
    assert area.key == key


def test_cancelled_and_default_collections(payload: dict[str, Any]) -> None:
    payload["status"] = "cancelled"
    for name in ("people", "areas", "themes", "language", "field_confidence", "inferred_fields"):
        del payload[name]
    first = EventImport.model_validate(payload)
    second = EventImport.model_validate(payload)
    assert first.status == "cancelled"
    first.themes.append("climate")
    assert second.themes == []
    assert first.people == second.people == []
    assert first.field_confidence is not second.field_confidence


@pytest.mark.parametrize("attribute", ["title", "confidence"])
def test_import_is_frozen(payload: dict[str, Any], attribute: str) -> None:
    event = EventImport.model_validate(payload)
    with pytest.raises(ValidationError, match="frozen_instance"):
        setattr(event, attribute, "Changed")


def test_batch_limit(payload: dict[str, Any]) -> None:
    assert len(EventImportBatch.model_validate({"items": [payload] * 50}).items) == 50
    with pytest.raises(ValidationError, match="too_long"):
        EventImportBatch.model_validate({"items": [payload] * 51})
    with pytest.raises(ValidationError, match="too_short"):
        EventImportBatch(items=[])


def test_batch_preserves_per_item_validation(payload: dict[str, Any]) -> None:
    batch = EventImportBatch.model_validate({"items": [payload, {"title": "Missing facts"}]})
    assert len(batch.items) == 2
    assert EventImport.model_validate(batch.items[0]).title == payload["title"]
    with pytest.raises(ValidationError):
        EventImport.model_validate(batch.items[1])
    assert EventImportBatch.model_validate_json(batch.model_dump_json()) == batch


@pytest.mark.parametrize("item", [None, False, 1, "invalid", []])
def test_batch_accepts_invalid_json_items_for_core_validation(item: Any) -> None:
    batch = EventImportBatch(items=[item])
    with pytest.raises(ValidationError):
        EventImport.model_validate(batch.items[0])


def test_batch_factory_dumps_imports_as_json(payload: dict[str, Any]) -> None:
    event = EventImport.model_validate(payload)
    batch = EventImportBatch.of([event])
    assert batch.items == [event.model_dump(mode="json")]
    assert EventImportBatch.model_validate_json(batch.model_dump_json()) == batch
    assert EventImport.model_validate(batch.items[0]) == event
    with pytest.raises(ValidationError, match="too_short"):
        EventImportBatch.of([])
    with pytest.raises(ValidationError, match="too_long"):
        EventImportBatch.of([event] * 51)


def test_per_item_import_results() -> None:
    response = EventImportResponse.model_validate(
        {
            "results": [
                {
                    "index": index,
                    "source_id": f"forge:{index}",
                    "result": result,
                    "event_id": "019a7080-1000-7000-8000-000000000001",
                    "review_reasons": ["unmapped_areas"] if result == "draft" else [],
                    "ignored_fields": ["title"] if result == "updated" else [],
                }
                for index, result in enumerate(["created", "updated", "unchanged", "draft"])
            ]
            + [
                {
                    "index": 4,
                    "result": "rejected",
                    "errors": [{"loc": ["source", "source_id"], "msg": "Field required"}],
                }
            ]
        }
    )
    assert response.results[-1].event_id is None
    assert response.results[-1].source_id is None
    assert EventImportResponse.model_validate_json(response.model_dump_json()) == response


@pytest.mark.parametrize(
    "changes",
    [
        {"index": -1},
        {"result": "skipped"},
        {"result": "review"},
        {"event_id": "invalid"},
        {"review_reasons": ["unknown"]},
        {"errors": [{"loc": ["title"]}]},
    ],
)
def test_invalid_import_result(changes: dict[str, Any]) -> None:
    payload = {"index": 0, "result": "created"} | changes
    with pytest.raises(ValidationError):
        EventImportResult.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [{"urls": ["file:///etc/passwd"]}, {"ends_at": "2026-11-08T18:00:00"}],
)
def test_invalid_tracked_event(changes: dict[str, Any]) -> None:
    payload = {
        "source_id": "forge:harbor",
        "urls": ["https://events.example.org/harbor"],
        "ends_at": "2026-11-08T18:00:00Z",
    } | changes
    with pytest.raises(ValidationError):
        TrackedEvent.model_validate(payload)


def test_tracked_events_and_areas() -> None:
    tracked = TrackedEventsResponse.model_validate(
        {
            "items": [
                {
                    "source_id": "forge:harbor",
                    "urls": ["https://events.example.org/harbor"],
                    "ends_at": "2026-11-08T18:00:00Z",
                }
            ],
            "next_cursor": "next-source",
        }
    )
    areas = AreaListResponse.model_validate(
        {
            "items": [
                {
                    "id": "019a7080-1000-7000-8000-000000000002",
                    "key": "backend",
                    "name": "Backend",
                    "description": "APIs and databases",
                    "sort": 0,
                }
            ],
            "next_cursor": None,
        }
    )
    assert TrackedEventsResponse.model_validate_json(tracked.model_dump_json()) == tracked
    assert AreaListResponse.model_validate_json(areas.model_dump_json()) == areas


@pytest.fixture
def heartbeat_payload() -> dict[str, Any]:
    return {
        "run_id": "nightly-2026-11-12",
        "started_at": "2026-11-12T01:00:00Z",
        "finished_at": "2026-11-12T01:14:00Z",
        "stats": {"sources_checked": 30, "candidates": 8, "imported": 6, "errors": 1},
        "errors": [{"source_id": "forge:unavailable", "message": "Request timed out"}],
    }


def test_heartbeat_round_trip(heartbeat_payload: dict[str, Any]) -> None:
    heartbeat = ParserHeartbeat.model_validate(heartbeat_payload)
    assert ParserHeartbeat.model_validate_json(heartbeat.model_dump_json()) == heartbeat


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
def test_heartbeat_requires_aware_times(heartbeat_payload: dict[str, Any], field: str) -> None:
    heartbeat_payload[field] = "2026-11-12T01:00:00"
    with pytest.raises(ValidationError, match="timezone_aware"):
        ParserHeartbeat.model_validate(heartbeat_payload)


@pytest.mark.parametrize("field", ["sources_checked", "candidates", "imported", "errors"])
def test_heartbeat_rejects_negative_counts(heartbeat_payload: dict[str, Any], field: str) -> None:
    heartbeat_payload["stats"][field] = -1
    with pytest.raises(ValidationError):
        ParserHeartbeat.model_validate(heartbeat_payload)


def test_public_models_inherit_contract() -> None:
    assert len(imports.__all__) == len(set(imports.__all__))
    for name in imports.__all__:
        value = getattr(imports, name)
        if isinstance(value, type):
            assert issubclass(value, Contract)
            assert value.model_config["frozen"]
            assert value.model_config["extra"] == "forbid"
