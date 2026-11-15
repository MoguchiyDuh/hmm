from datetime import datetime, timedelta
from typing import Any

import pytest
from factories import new_row
from harness import Harness
from pydantic import ValidationError
from sqlalchemy import select

from hmm import models
from hmm.catalog import parser
from hmm.catalog.import_schema import ParserHeartbeat, TrackedEventsResponse
from hmm.common import ErrorCode
from hmm.notifications.payloads import AlertKind
from hmm.notifications.service import raise_alert
from hmm.platform.errors import ProblemError
from hmm.platform.pagination import CursorParams


async def seed_source(h: Harness, name: str, ends_at: datetime, status: str) -> None:
    async with h.db() as session:
        event = new_row(
            models.Event,
            slug=name,
            title=name.title(),
            starts_at=ends_at - timedelta(days=1),
            ends_at=ends_at,
            format="offline",
            status=status,
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


async def tracked(h: Harness, cursor: str | None = None, limit: int = 20) -> TrackedEventsResponse:
    async with h.db() as session:
        return await parser.tracked_events(session, CursorParams(cursor=cursor, limit=limit), h.now)


async def heartbeat(h: Harness, body: dict[str, Any]) -> None:
    async with h.db() as session:
        await parser.record_heartbeat(session, ParserHeartbeat.model_validate(body))


async def test_tracked_events_window(harness: Harness) -> None:
    h = harness
    await seed_window(h)
    body = (await tracked(h)).model_dump(mode="json")
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
    assert (await tracked(h)).model_dump(mode="json") == {"items": [], "next_cursor": None}
    await seed_window(h)
    page = await tracked(h, limit=2)
    assert len(page.items) == 2
    assert page.next_cursor is not None
    rest = await tracked(h, cursor=page.next_cursor, limit=2)
    assert rest.next_cursor is None
    ids = [item.source_id for item in page.items + rest.items]
    assert ids == ["src:ended", "src:draft", "src:later"]
    with pytest.raises(ProblemError) as raised:
        await tracked(h, cursor="garbage")
    assert raised.value.code is ErrorCode.BAD_REQUEST


async def test_parser_heartbeat_upsert(harness: Harness) -> None:
    h = harness
    await heartbeat(h, heartbeat_body(h))
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
    await heartbeat(h, heartbeat_body(h, imported=3))
    async with h.db() as session:
        rows = list(await session.scalars(select(models.ParserHeartbeat)))
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert isinstance(rows[0].stats, dict)
    assert rows[0].stats["imported"] == 3
    await heartbeat(h, heartbeat_body(h, run_id="r2"))
    async with h.db() as session:
        run_ids = sorted(await session.scalars(select(models.ParserHeartbeat.run_id)))
    assert run_ids == ["r1", "r2"]


async def test_parser_endpoints_leave_parser_down_alone(harness: Harness) -> None:
    h = harness
    await seed_window(h)
    assert len((await tracked(h)).items) == 3
    await heartbeat(h, heartbeat_body(h))
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
    assert len((await tracked(h)).items) == 3
    await heartbeat(h, heartbeat_body(h, run_id="r2"))
    async with h.db() as session:
        alerts = list(await session.scalars(select(models.AdminAlert)))
    assert [(row.id, row.type, row.status) for row in alerts] == [(alert_id, "parser_down", "open")]


async def test_parser_heartbeat_validation(harness: Harness) -> None:
    h = harness
    body = heartbeat_body(h)
    del body["stats"]
    with pytest.raises(ValidationError):
        ParserHeartbeat.model_validate(body)
    async with h.db() as session:
        assert list(await session.scalars(select(models.ParserHeartbeat))) == []
