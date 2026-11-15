from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
from harness import Harness
from parser_support import Router, fixture, json_file, no_sleep, public_resolve, respond
from sqlalchemy import func, select

from hmm import models
from hmm.catalog.import_schema import ParserHeartbeat
from hmm.parser.jobs import run_parser

SOURCES = """
[[source]]
id = "ctftime"
adapter = "ctftime"
interval = "12h"
include_online = false

[[source]]
id = "bbicc"
adapter = "jsonld_pages"
interval = "24h"
countries = ["RS"]
kind = "contest"
guess_kind = false
confidence = 0.85
follow = 'https://bbicc\\.fon\\.bg\\.ac\\.rs/bbicc-\\d{4}/(?=<)'
listings = ["https://bbicc.fon.bg.ac.rs/wp-sitemap-posts-page-1.xml"]

[[source]]
id = "broken"
adapter = "jsonld_pages"
listings = ["https://down.example/"]

[[source]]
id = "secret"
adapter = "ctftime"
secret_env = ["NOT_SET_TOKEN"]

[[source]]
id = "off"
adapter = "ctftime"
enabled = false
"""


def router() -> Router:
    return Router(
        {
            "https://ctftime.org/api/v1/events/": json_file("ctftime/events.json"),
            "https://bbicc.fon.bg.ac.rs/wp-sitemap-posts-page-1.xml": respond(
                "jsonld/bbicc_sitemap.xml", "application/xml"
            ),
            "https://bbicc.fon.bg.ac.rs/bbicc-2027/": respond("jsonld/bbicc_2027.html"),
            "https://down.example/": lambda _: httpx.Response(410),
        }
    )


async def run(harness: Harness, tmp_path: Path, routes: Router) -> ParserHeartbeat:
    path = tmp_path / "sources.toml"
    path.write_text(SOURCES)
    config = harness.runtime.config.model_copy(update={"parser_sources": str(path)})
    async with httpx.AsyncClient(transport=routes.transport()) as http:
        harness.inject_http(http)
        harness.runtime = replace(harness.runtime, config=config)
        heartbeat = await run_parser(
            harness.runtime, env={}, resolve=public_resolve, sleep=no_sleep
        )
    return heartbeat


async def test_parser_run_imports_and_records_state(harness: Harness, tmp_path: Path) -> None:
    await harness.seed_areas()
    routes = router()
    heartbeat = await run(harness, tmp_path, routes)
    assert heartbeat.stats.sources_checked == 3
    assert heartbeat.stats.imported == 3
    assert heartbeat.stats.errors == 1
    assert heartbeat.errors == [
        {
            "source": "broken",
            "stage": "discover",
            "error": "FetchError: HTTP 410: https://down.example/",
        }
    ]
    async with harness.db() as session:
        sources = set(await session.scalars(select(models.EventSource.source_id)))
        assert sources == {"ctftime:3444", "ctftime:3505", "bbicc:bbicc-2027"}
        bbicc = await session.scalar(
            select(models.Event)
            .join(models.EventSource, models.EventSource.event_id == models.Event.id)
            .where(models.EventSource.source_id == "bbicc:bbicc-2027")
        )
        assert bbicc is not None
        assert bbicc.status == "published"
        assert bbicc.kind == "contest"
        states = {
            row.source_id: row for row in await session.scalars(select(models.ParserSourceState))
        }
        assert set(states) == {"ctftime", "bbicc", "broken"}
        assert states["ctftime"].last_success_at == harness.now
        assert states["ctftime"].stats == {
            "candidates": 7,
            "items": 2,
            "dropped": 0,
            "errors": 0,
            "revisited": 0,
            "gone": 0,
            "rejected": 0,
        }
        assert states["broken"].last_success_at is None
        assert "HTTP 410" in (states["broken"].last_error or "")
        stored = await session.scalar(select(func.count()).select_from(models.ParserHeartbeat))
        assert stored == 1


async def test_parser_run_respects_intervals(harness: Harness, tmp_path: Path) -> None:
    await harness.seed_areas()
    await run(harness, tmp_path, router())
    harness.advance(timedelta(minutes=30))
    idle = await run(harness, tmp_path, quiet := router())
    assert idle.stats.sources_checked == 0
    assert quiet.requests == []
    harness.advance(timedelta(hours=12))
    again = await run(harness, tmp_path, router())
    assert again.stats.sources_checked == 2
    assert again.stats.imported == 2
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ParserHeartbeat)) == 3
        assert await session.scalar(select(func.count()).select_from(models.EventSource)) == 3


SITEMAP = "https://bbicc.fon.bg.ac.rs/wp-sitemap-posts-page-1.xml"
PAGE = "https://bbicc.fon.bg.ac.rs/bbicc-2027/"


def cancelled(_: httpx.Request) -> httpx.Response:
    body = fixture("jsonld/bbicc_2027.html").replace(b"EventScheduled", b"EventCancelled")
    return httpx.Response(200, content=body, headers={"content-type": "text/html"})


def unlisted(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=b"<urlset></urlset>", headers={"content-type": "application/xml"}
    )


async def bbicc_status(harness: Harness) -> str | None:
    async with harness.db() as session:
        return await session.scalar(
            select(models.Event.status)
            .join(models.EventSource, models.EventSource.event_id == models.Event.id)
            .where(models.EventSource.source_id == "bbicc:bbicc-2027")
        )


async def test_parser_run_revisits_unlisted_tracked_events(
    harness: Harness, tmp_path: Path
) -> None:
    await harness.seed_areas()
    await run(harness, tmp_path, router())
    harness.advance(timedelta(hours=24))
    routes = router()
    routes.routes[SITEMAP] = unlisted
    routes.routes[PAGE] = cancelled
    await run(harness, tmp_path, routes)
    assert [str(request.url) for request in routes.requests].count(PAGE) == 1
    assert await bbicc_status(harness) == "cancelled"
    async with harness.db() as session:
        revisit = await session.get(models.ParserRevisit, "bbicc:bbicc-2027")
        assert revisit is not None
        assert (revisit.checked_at, revisit.misses) == (harness.now, 0)
        state = await session.get(models.ParserSourceState, "bbicc")
        assert state is not None
        assert state.stats == {
            "candidates": 0,
            "items": 1,
            "dropped": 0,
            "errors": 0,
            "revisited": 1,
            "gone": 0,
            "rejected": 0,
        }
    harness.advance(timedelta(hours=24))
    gone = router()
    gone.routes[SITEMAP] = unlisted
    del gone.routes[PAGE]
    await run(harness, tmp_path, gone)
    assert await bbicc_status(harness) == "cancelled"
    async with harness.db() as session:
        revisit = await session.get(models.ParserRevisit, "bbicc:bbicc-2027")
        assert revisit is not None
        assert (revisit.checked_at, revisit.misses) == (harness.now, 1)


async def test_parser_run_skips_cancellations_of_unknown_events(
    harness: Harness, tmp_path: Path
) -> None:
    await harness.seed_areas()
    routes = router()
    routes.routes[PAGE] = cancelled
    heartbeat = await run(harness, tmp_path, routes)
    assert heartbeat.stats.imported == 2
    assert await bbicc_status(harness) is None
