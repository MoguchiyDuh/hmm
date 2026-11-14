from datetime import datetime, timedelta
from typing import Any

from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.notifications.service import raise_alert
from sqlalchemy import select
from test_api_skeleton import assert_problem

TRACKED_URL = "/api/v1/internal/events/tracked"
HEARTBEAT_URL = "/api/v1/internal/parser/heartbeat"


async def seed_source(h: Harness, name: str, ends_at: datetime, status: str) -> None:
    async with h.db() as session:
        event = models.Event(
            slug=name,
            kind="hackathon",
            formation_enabled=False,
            title=name.title(),
            starts_at=ends_at - timedelta(days=1),
            ends_at=ends_at,
            timezone="UTC",
            format="offline",
            language=[],
            themes=[],
            status=status,
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        session.add(
            models.EventSource(
                event_id=event.id,
                source_id=f"src:{name}",
                urls=[f"https://events.example.org/{name}"],
                payload={},
                payload_hash="h",
                extractor_version="v",
                first_seen_at=h.now,
                last_seen_at=h.now,
            )
        )


async def seed_window(h: Harness) -> None:
    for name, offset, status in (
        ("ended", timedelta(days=-5), "published"),
        ("draft", timedelta(days=1), "draft"),
        ("later", timedelta(days=5), "published"),
        ("old", timedelta(days=-40), "published"),
        ("archived", timedelta(days=3), "archived"),
    ):
        await seed_source(h, name, h.now + offset, status)


def heartbeat_body(h: Harness, run_id: str = "r1", imported: int = 2) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": (h.now - timedelta(minutes=10)).isoformat(),
        "finished_at": h.now.isoformat(),
        "stats": {"sources_checked": 3, "candidates": 5, "imported": imported, "errors": 0},
        "errors": [{"source": "x", "message": "boom"}],
    }


async def test_tracked_events_window(harness: Harness) -> None:
    h = harness
    await seed_window(h)
    async with h.parser() as client:
        response = await client.get(TRACKED_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    assert body["items"] == [
        {
            "source_id": f"src:{name}",
            "urls": [f"https://events.example.org/{name}"],
            "ends_at": (h.now + offset).isoformat().replace("+00:00", "Z"),
        }
        for name, offset in (
            ("ended", timedelta(days=-5)),
            ("draft", timedelta(days=1)),
            ("later", timedelta(days=5)),
        )
    ]


async def test_tracked_events_cursor(harness: Harness) -> None:
    h = harness
    async with h.parser() as client:
        response = await client.get(TRACKED_URL)
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "next_cursor": None}
        await seed_window(h)
        first = await client.get(TRACKED_URL, params={"limit": 2})
        assert first.status_code == 200, first.text
        page = first.json()
        assert len(page["items"]) == 2
        assert page["next_cursor"] is not None
        second = await client.get(TRACKED_URL, params={"limit": 2, "cursor": page["next_cursor"]})
        assert second.status_code == 200, second.text
        rest = second.json()
        assert rest["next_cursor"] is None
        ids = [item["source_id"] for item in page["items"] + rest["items"]]
        assert ids == ["src:ended", "src:draft", "src:later"]
        response = await client.get(TRACKED_URL, params={"cursor": "garbage"})
    assert_problem(response, 400, ErrorCode.BAD_REQUEST)


async def test_parser_heartbeat_upsert(harness: Harness) -> None:
    h = harness
    async with h.parser() as client:
        response = await client.post(HEARTBEAT_URL, json=heartbeat_body(h))
        assert response.status_code == 204, response.text
        async with h.db() as session:
            rows = list(await session.scalars(select(models.ParserHeartbeat)))
        assert len(rows) == 1
        first_id = rows[0].id
        assert rows[0].run_id == "r1"
        assert rows[0].started_at == h.now - timedelta(minutes=10)
        assert rows[0].finished_at == h.now
        assert rows[0].stats == {
            "sources_checked": 3,
            "candidates": 5,
            "imported": 2,
            "errors": 0,
        }
        assert rows[0].errors == [{"source": "x", "message": "boom"}]
        response = await client.post(HEARTBEAT_URL, json=heartbeat_body(h, imported=3))
        assert response.status_code == 204, response.text
        async with h.db() as session:
            rows = list(await session.scalars(select(models.ParserHeartbeat)))
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert isinstance(rows[0].stats, dict)
        assert rows[0].stats["imported"] == 3
        response = await client.post(HEARTBEAT_URL, json=heartbeat_body(h, run_id="r2"))
        assert response.status_code == 204, response.text
    async with h.db() as session:
        run_ids = sorted(await session.scalars(select(models.ParserHeartbeat.run_id)))
    assert run_ids == ["r1", "r2"]


async def test_parser_endpoints_leave_parser_down_alone(harness: Harness) -> None:
    h = harness
    await seed_window(h)
    async with h.parser() as client:
        assert (await client.get(TRACKED_URL)).status_code == 200
        assert (await client.post(HEARTBEAT_URL, json=heartbeat_body(h))).status_code == 204
    async with h.db() as session:
        alerts = list(await session.scalars(select(models.AdminAlert)))
        assert alerts == []
        alert = await raise_alert(
            session,
            AlertKind.PARSER_DOWN,
            h.now,
            title="Parser is down",
            message="No heartbeat",
            link="/admin/system",
            subject_type="parser",
            dedupe_key="parser_down",
        )
        assert alert is not None
        alert_id = alert.id
    async with h.parser() as client:
        assert (await client.get(TRACKED_URL)).status_code == 200
        response = await client.post(HEARTBEAT_URL, json=heartbeat_body(h, run_id="r2"))
        assert response.status_code == 204
    async with h.db() as session:
        alerts = list(await session.scalars(select(models.AdminAlert)))
    assert [(row.id, row.type, row.status) for row in alerts] == [(alert_id, "parser_down", "open")]


async def test_parser_endpoints_auth_and_validation(harness: Harness) -> None:
    h = harness
    async with h.anonymous() as client:
        assert_problem(await client.get(TRACKED_URL), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await client.post(HEARTBEAT_URL, json=heartbeat_body(h)),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
    async with h.bot() as client:
        assert_problem(await client.get(TRACKED_URL), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await client.post(HEARTBEAT_URL, json=heartbeat_body(h)),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
    body = heartbeat_body(h)
    del body["stats"]
    async with h.parser() as client:
        response = await client.post(HEARTBEAT_URL, json=body)
    assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    async with h.db() as session:
        assert list(await session.scalars(select(models.ParserHeartbeat))) == []
