import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from parser_support import Router, json_file, respond, run_source

from hmm.catalog.import_schema import TrackedEvent
from hmm.common import EventFormat, EventKind

HH_SITE = "https://hackathonhub.eu"
HH_API = "https://abcdefghijklmnop.supabase.co/rest/v1/events_public"
HH_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJyb2xlIjoiYW5vbiIsImZha2UiOnRydWV9"
    ".ZmFrZXNpZ25hdHVyZWZvcnRlc3Rz"
)
GDG = "https://gdg.community.dev"


def by_id(items: list[Any]) -> dict[str, Any]:
    return {item.source.source_id: item for item in items}


def hackathonhub_router() -> Router:
    return Router(
        {
            f"{HH_SITE}/": respond("hackathonhub/home.html"),
            f"{HH_SITE}/assets/index-DhDhOlAv.js": respond(
                "hackathonhub/bundle.js", "text/javascript"
            ),
            HH_API: json_file("hackathonhub/rows.json"),
        }
    )


async def test_hackathonhub_discovers_key_and_maps_rows() -> None:
    router = hackathonhub_router()
    run = await run_source(
        {"id": "hackathonhub", "adapter": "hackathonhub", "countries": ["RS", "BG"]}, router
    )
    assert run.errors == []
    api = next(request for request in router.requests if request.url.host.endswith("supabase.co"))
    assert api.headers["apikey"] == HH_KEY
    assert api.headers["authorization"] == f"Bearer {HH_KEY}"
    assert api.headers["range"] == "0-999"
    assert api.url.params["end_date"] == "gte.2026-10-16"
    items = by_id(run.items)
    dev3 = items["hackathonhub:55779a6b-0978-4fe9-acf3-e35650aa7daf"]
    assert dev3.kind == EventKind.HACKATHON
    assert dev3.format == EventFormat.HYBRID
    assert dev3.timezone == "Europe/Belgrade"
    assert dev3.starts_at == datetime(2026, 11, 26, 23, tzinfo=UTC)
    assert dev3.ends_at == datetime(2026, 11, 29, 22, 59, 59, tzinfo=UTC)
    assert dev3.prize is not None and (dev3.prize.amount, dev3.prize.currency) == (10000.0, "USD")
    assert dev3.eligibility == "Students only"
    assert dev3.language == ["en"]
    assert str(dev3.website_url).endswith("/events/dev3pack-global-hackathon-belgrade-2026")
    assert dev3.confidence == 0.85
    assert {"timezone", "starts_at", "ends_at"} <= set(dev3.inferred_fields)
    jam = items["hackathonhub:b0f070f9-f8fa-4337-ad15-16ac48c8c413"]
    assert jam.kind == EventKind.CONTEST
    assert jam.location is not None and jam.location.country == "BG"
    assert jam.areas[0].key == "gamedev"
    assert jam.confidence == 0.8
    adapty = items["hackathonhub:22fa08c3-8193-4e22-8164-e96b87f5d30f"]
    assert adapty.starts_at == datetime(2026, 10, 24, 10, 30, tzinfo=UTC)
    assert "starts_at" not in adapty.inferred_fields
    online = items["hackathonhub:0f582371-1449-4e30-bdb3-94b57ce4d1b1"]
    assert online.format == EventFormat.ONLINE
    assert "hackathonhub:b5cd12b1-03da-452f-8683-373d8f63f77a" not in items
    assert all(
        item.location is None or item.location.country in {"RS", "BG"} or item.format != "offline"
        for item in run.items
    )


async def test_hackathonhub_uses_configured_key_and_pages() -> None:
    pages: list[str] = []

    def rows(request: httpx.Request) -> httpx.Response:
        pages.append(request.headers["range"])
        start = int(request.headers["range"].split("-")[0])
        body = json_file("hackathonhub/rows.json")(request).json()
        return httpx.Response(206 if start == 0 else 200, json=body[start : start + 5])

    router = Router({HH_API: rows})
    run = await run_source(
        {
            "id": "hackathonhub",
            "adapter": "hackathonhub",
            "api_url": "https://abcdefghijklmnop.supabase.co",
            "page_size": 5,
        },
        router,
        env={"HACKATHONHUB_ANON_KEY": "configured"},
    )
    assert run.errors == []
    assert pages == ["0-4", "5-9"]
    assert run.candidates == 8
    assert all(request.headers["apikey"] == "configured" for request in router.requests)


async def test_hackathonhub_missing_bundle_is_a_source_error() -> None:
    router = Router({f"{HH_SITE}/": lambda _: httpx.Response(200, text="<html></html>")})
    run = await run_source({"id": "hackathonhub", "adapter": "hackathonhub"}, router)
    assert run.items == []
    assert run.errors[0]["stage"] == "discover"
    assert "bundle not found" in str(run.errors[0]["error"])


async def test_hackathonist_parses_markdown_tables() -> None:
    router = Router(
        {
            "https://raw.githubusercontent.com/zsevic/hackathonist/gh-pages/README.md": respond(
                "hackathonist/README.md", "text/plain; charset=utf-8"
            )
        }
    )
    run = await run_source(
        {"id": "hackathonist", "adapter": "hackathonist", "lookback_days": 400}, router
    )
    assert run.errors == []
    assert run.candidates == 15
    titles = {item.title: item for item in run.items}
    assert len(titles) == 15
    rivian = titles["Rivian Hakaton"]
    assert rivian.format == EventFormat.HYBRID
    assert rivian.location is not None and rivian.location.address == "Rivian HQ"
    assert rivian.starts_at == datetime(2026, 11, 5, 23, tzinfo=UTC)
    assert rivian.ends_at == datetime(2026, 11, 7, 22, 59, 59, tzinfo=UTC)
    frontier = titles["Frontier Hackathon"]
    assert frontier.location is not None and frontier.location.city == "Belgrade"
    assert frontier.format == EventFormat.OFFLINE
    money = titles["Money Agent Hackathon"]
    assert money.starts_at.date().isoformat() == "2026-04-24"
    assert money.ends_at.date().isoformat() == "2026-05-29"
    grok = titles["Grok Bot Serbia Hackathon"]
    assert grok.starts_at == datetime(2026, 9, 11, 22, tzinfo=UTC)
    fintech, frontier_id = (
        titles["FinTech AI Hackathon"].source.source_id,
        frontier.source.source_id,
    )
    assert fintech != frontier_id
    assert frontier_id.endswith("-20261107")


async def test_hackathonist_drops_old_rows() -> None:
    router = Router(
        {
            "https://raw.githubusercontent.com/zsevic/hackathonist/gh-pages/README.md": respond(
                "hackathonist/README.md", "text/plain"
            )
        }
    )
    run = await run_source({"id": "hackathonist", "adapter": "hackathonist"}, router)
    assert sorted(item.title for item in run.items) == ["Frontier Hackathon", "Rivian Hakaton"]


async def test_ctftime_maps_events_and_scope() -> None:
    router = Router({"https://ctftime.org/api/v1/events/": json_file("ctftime/events.json")})
    run = await run_source(
        {"id": "ctftime", "adapter": "ctftime", "rate": "1/10s", "window_days": 120}, router
    )
    assert run.errors == []
    params = router.requests[0].url.params
    assert params["start"] == str(int((datetime(2026, 10, 16, 12, tzinfo=UTC)).timestamp()))
    assert params["finish"] == str(int((datetime(2027, 3, 15, 12, tzinfo=UTC)).timestamp()))
    items = by_id(run.items)
    assert "ctftime:3409" not in items
    italy = items["ctftime:3444"]
    assert italy.format == EventFormat.OFFLINE
    assert italy.location is not None and italy.location.country == "IT"
    assert italy.timezone == "Europe/Rome"
    assert italy.eligibility == "Prequalified"
    lemon = items["ctftime:3485"]
    assert lemon.format == EventFormat.ONLINE
    assert (lemon.team_size_min, lemon.team_size_max) == (1, 4)
    assert lemon.areas[0].key == "security"
    assert str(lemon.registration_url) == "https://ctftime.org/event/3485/"
    assert items["ctftime:3505"].summary is not None
    assert not items["ctftime:3505"].summary.startswith("#")
    assert all(item.kind == EventKind.CONTEST and item.confidence == 0.85 for item in run.items)


async def test_ctftime_without_online() -> None:
    router = Router({"https://ctftime.org/api/v1/events/": json_file("ctftime/events.json")})
    run = await run_source({"id": "ctftime", "adapter": "ctftime", "include_online": False}, router)
    assert sorted(item.source.source_id for item in run.items) == ["ctftime:3444", "ctftime:3505"]


def luma_router() -> Router:
    def items(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        calendar, period = params["calendar_api_id"], params["period"]
        if calendar == "cal-xfRCBuSHDpIoEmp" and period == "future":
            return json_file("luma/aisalon_future.json")(request)
        if calendar == "cal-WcgY6wHZ5tA68xg" and period == "past":
            page = "2" if params.get("pagination_cursor") == "cursor-page-2" else "1"
            return json_file(f"luma/garaza_past_{page}.json")(request)
        return json_file("luma/empty.json")(request)

    return Router(
        {
            "https://api.lu.ma/calendar/get-items": items,
            "https://api.lu.ma/event/get": json_file("luma/event.json"),
        }
    )


async def test_luma_filters_country_and_converts_description() -> None:
    router = luma_router()
    run = await run_source(
        {
            "id": "luma-rs",
            "adapter": "luma",
            "countries": ["RS"],
            "include_online": False,
            "calendars": ["cal-WcgY6wHZ5tA68xg", "cal-xfRCBuSHDpIoEmp"],
        },
        router,
    )
    assert run.errors == []
    items = by_id(run.items)
    assert set(items) == {"luma-rs:evt-o9DNH6twxVs8voS", "luma-rs:evt-22xY3VCXEXei2rN"}
    salon = items["luma-rs:evt-o9DNH6twxVs8voS"]
    assert salon.timezone == "Europe/Belgrade"
    assert salon.location is not None and salon.location.city == "Belgrade"
    assert salon.language == ["sr"]
    assert salon.organizer == "AI Salon (www.AiSalon.ai)"
    assert salon.description_md is not None
    assert salon.description_md.startswith("[AiSalon](https://aisalon.ai/?utm_source=luma) je")
    assert "# O kompaniji Wonderful" in salon.description_md
    assert "**Wonderful**" in salon.description_md
    assert salon.summary is not None and salon.summary.startswith("AiSalon je globalna")
    assert salon.areas[0].key == "ml_ai"
    assert items["luma-rs:evt-22xY3VCXEXei2rN"].organizer == "SaaStanak"
    detail_calls = [r for r in router.requests if r.url.path == "/event/get"]
    assert len(detail_calls) == 2
    past_calls = [
        r
        for r in router.requests
        if r.url.params.get("calendar_api_id") == "cal-WcgY6wHZ5tA68xg"
        and r.url.params.get("period") == "past"
    ]
    assert len(past_calls) == 2


def gdg_router() -> Router:
    detail = f"{GDG}/events/details/google-gdg-belgrade-presents"
    missing = {
        f"{detail}-devfest-2026-conference/",
        f"{detail}-gdg-belgrade-open-day/",
        f"{detail}-mentorship-program-202627-opening-ceremony/",
    }
    return Router(
        {
            f"{GDG}/gdg-belgrade/": respond("jsonld/gdg_chapter.html"),
            f"{detail}-devfest-in-the-cloud/": respond("jsonld/gdg_event.html"),
            f"{detail}-devfest-2026-hackathon/": respond("jsonld/gdg_hackathon.html"),
            **{url: lambda _: httpx.Response(200, text="<html></html>") for url in missing},
        }
    )


async def test_jsonld_pages_follows_links() -> None:
    router = gdg_router()
    run = await run_source(
        {
            "id": "gdg-rs",
            "adapter": "jsonld_pages",
            "countries": ["RS"],
            "confidence": 0.85,
            "follow": r"https://gdg\.community\.dev/events/details/[a-z0-9-]+/",
            "listings": [f"{GDG}/gdg-belgrade/"],
            "lookback_days": 60,
        },
        router,
    )
    assert run.errors == []
    assert run.candidates == 5
    items = by_id(run.items)
    cloud = items["gdg-rs:google-gdg-belgrade-presents-devfest-in-the-cloud"]
    assert cloud.kind == EventKind.CONFERENCE
    assert cloud.starts_at == datetime(2026, 12, 12, 9, tzinfo=UTC)
    assert cloud.organizer is None
    assert cloud.location is not None
    assert (cloud.location.country, cloud.location.city) == ("RS", "Belgrade")
    assert cloud.location.address == "Faculty of Organizational Sciences, 154 Jove Ilića"
    assert cloud.registration_url is None
    assert cloud.registration_deadline is None
    assert cloud.inferred_fields == ["timezone"]
    hackathon = items["gdg-rs:google-gdg-belgrade-presents-devfest-2026-hackathon"]
    assert hackathon.kind == EventKind.HACKATHON
    assert str(hackathon.registration_url).startswith("https://forms.gdg-belgrade.com/")
    assert all(item.confidence == 0.85 for item in run.items)


async def test_jsonld_pages_inline_listing_with_id_pattern() -> None:
    router = Router({"https://dev.events/EU/RS": respond("jsonld/devevents_rs.html")})
    run = await run_source(
        {
            "id": "dev-events",
            "adapter": "jsonld_pages",
            "kind": "conference",
            "include_online": False,
            "id_pattern": "-([a-z0-9]{8})$",
            "listings": ["https://dev.events/EU/RS"],
        },
        router,
    )
    assert run.errors == []
    items = by_id(run.items)
    assert set(items) == {"dev-events:tpzpweng", "dev-events:drviqcgk", "dev-events:at8pb2mc"}
    devfest = items["dev-events:tpzpweng"]
    assert devfest.starts_at == datetime(2026, 12, 2, 23, tzinfo=UTC)
    assert devfest.ends_at == datetime(2026, 12, 4, 22, 59, 59, tzinfo=UTC)
    assert devfest.location is not None and devfest.location.country == "RS"
    assert {"starts_at", "ends_at", "timezone"} <= set(devfest.inferred_fields)
    assert items["dev-events:drviqcgk"].areas[0].key == "qa"
    assert items["dev-events:at8pb2mc"].areas[0].key == "ml_ai"
    assert len(router.requests) == 1


async def test_jsonld_pages_graph_and_defaults() -> None:
    router = Router(
        {
            "https://bbicc.fon.bg.ac.rs/wp-sitemap-posts-page-1.xml": respond(
                "jsonld/bbicc_sitemap.xml", "application/xml"
            ),
            "https://bbicc.fon.bg.ac.rs/bbicc-2027/": respond("jsonld/bbicc_2027.html"),
        }
    )
    run = await run_source(
        {
            "id": "bbicc",
            "adapter": "jsonld_pages",
            "kind": "contest",
            "guess_kind": False,
            "follow": r"https://bbicc\.fon\.bg\.ac\.rs/bbicc-\d{4}/(?=<)",
            "listings": ["https://bbicc.fon.bg.ac.rs/wp-sitemap-posts-page-1.xml"],
            "defaults": {"team_size_min": 4, "team_size_max": 4, "eligibility": "Undergrads"},
        },
        router,
    )
    assert run.errors == []
    assert run.candidates == 1
    [item] = run.items
    assert item.source.source_id == "bbicc:bbicc-2027"
    assert item.kind == EventKind.CONTEST
    assert item.organizer == "Belgrade Business International Case Competition"
    assert (item.team_size_min, item.team_size_max, item.eligibility) == (4, 4, "Undergrads")
    assert item.starts_at == datetime(2027, 3, 6, 23, tzinfo=UTC)
    assert item.ends_at - item.starts_at == timedelta(days=7) - timedelta(seconds=1)


ICPC_API = "https://icpc.global/api/contest/public"
ICPC_REGIONS = ("seerc", "central-europe", "northwestern-europe", "swerc")


async def test_icpc_imports_one_event_per_site() -> None:
    router = Router({f"{ICPC_API}/{slug}": json_file(f"icpc/{slug}.json") for slug in ICPC_REGIONS})
    run = await run_source({"id": "icpc", "adapter": "icpc"}, router)
    assert run.errors == []
    assert run.candidates == 4
    items = by_id(run.items)
    assert set(items) == {
        "icpc:9565:39817",
        "icpc:9565:39816",
        "icpc:9588:40036",
        "icpc:9592:41856",
    }
    greece = items["icpc:9565:39817"]
    assert greece.title == "The 2026 ICPC Southeastern Europe Regional Contest \u2014 Thessaloniki"
    assert greece.kind == EventKind.CONTEST
    assert greece.format == EventFormat.OFFLINE
    assert greece.location is not None
    assert (greece.location.country, greece.location.city) == ("GR", "Thessaloniki")
    assert greece.timezone == "Europe/Athens"
    assert greece.starts_at == datetime(2026, 12, 10, 22, tzinfo=UTC)
    assert greece.ends_at == datetime(2026, 12, 13, 21, 59, 59, tzinfo=UTC)
    assert greece.registration_opens_at == datetime(2026, 10, 21, 21, tzinfo=UTC)
    assert greece.registration_deadline == datetime(2026, 11, 20, 21, 59, 59, tzinfo=UTC)
    assert (greece.team_size_min, greece.team_size_max) == (3, 3)
    assert {"team_size_min", "team_size_max", "starts_at"} <= set(greece.inferred_fields)
    assert greece.eligibility == "University teams from Southeastern Europe"
    assert str(greece.website_url) == "https://icpc.global/regionals/finder/seerc"
    assert items["icpc:9565:39816"].title.endswith("\u2014 Kyiv")
    cerc = items["icpc:9588:40036"]
    assert cerc.title == "The 2026 ICPC Central Europe Regional Contest"
    assert cerc.location is not None and cerc.location.country == "PL"
    swerc = items["icpc:9592:41856"]
    assert swerc.location is not None and swerc.location.country == "IT"


async def test_icpc_skips_regions_without_a_contest() -> None:
    router = Router({f"{ICPC_API}/seerc": json_file("icpc/seerc.json")})
    run = await run_source(
        {"id": "icpc", "adapter": "icpc", "regions": ["seerc", "missing"]}, router
    )
    assert run.errors == []
    assert len(run.items) == 2


MLH_SEASON = "https://www.mlh.com/seasons/2027/events"
MLH_PAGES = {
    (): "mlh/season.html",
    (("upcoming_page", "2"),): "mlh/upcoming_2.html",
    (("page", "2"),): "mlh/past_2.html",
    (("page", "3"),): "mlh/past_3.html",
}


def mlh_season(request: httpx.Request) -> httpx.Response:
    name = MLH_PAGES.get(tuple(request.url.params.multi_items()))
    if name is None:
        return httpx.Response(500)
    return respond(name)(request)


async def test_mlh_pages_html_lists_and_filters_regions() -> None:
    router = Router({MLH_SEASON: mlh_season})
    run = await run_source({"id": "mlh", "adapter": "mlh"}, router)
    assert run.errors == []
    assert [str(request.url) for request in router.requests] == [
        MLH_SEASON,
        f"{MLH_SEASON}?upcoming_page=2",
        f"{MLH_SEASON}?page=2",
        f"{MLH_SEASON}?page=3",
        "https://www.mlh.com/seasons/2028/events",
    ]
    items = by_id(run.items)
    assert set(items) == {
        "mlh:01a063b5-6f38-3b26-77aa-c9afde29ba75",
        "mlh:01a1169b-c4ad-5d41-61cf-deccaa37655c",
        "mlh:01a1456b-e3fd-791d-2d70-f96efa81449e",
        "mlh:01a0f9ce-2aec-1d0e-a9c8-f5b4213b0d1b",
        "mlh:01a1a71c-fe3c-68ff-32da-e25d4d01eeb3",
        "mlh:019ff171-24a3-0dd9-42c5-eabf54b0893c",
    }
    unihack = items["mlh:01a063b5-6f38-3b26-77aa-c9afde29ba75"]
    assert unihack.starts_at == datetime(2026, 11, 13, 13, tzinfo=UTC)
    assert unihack.ends_at == datetime(2026, 11, 15, 15, tzinfo=UTC)
    assert unihack.location is not None and unihack.location.country == "RO"
    assert unihack.timezone == "Europe/Bucharest"
    assert {"starts_at", "ends_at"} <= set(unihack.inferred_fields)
    burgh = items["mlh:01a0f9ce-2aec-1d0e-a9c8-f5b4213b0d1b"]
    assert burgh.starts_at == datetime(2026, 11, 7, 9, tzinfo=UTC)
    assert str(unihack.website_url) == "https://unihack.eu/"
    hackeps = items["mlh:01a1169b-c4ad-5d41-61cf-deccaa37655c"]
    assert hackeps.starts_at == datetime(2026, 11, 27, 23, tzinfo=UTC)
    assert hackeps.ends_at == datetime(2026, 11, 29, 22, 59, 59, tzinfo=UTC)
    assert {"starts_at", "ends_at"} <= set(hackeps.inferred_fields)
    solana = items["mlh:01a1a71c-fe3c-68ff-32da-e25d4d01eeb3"]
    assert solana.format == EventFormat.ONLINE
    assert solana.location is None
    assert items["mlh:01a1456b-e3fd-791d-2d70-f96efa81449e"].themes == ["MLH", "Software"]


HK_MCP = "https://hackalendar.com/api/mcp"
HK_DETAIL = "artefact-adopt-ai-hackathon-adopt-ai-for-health-paris-2026"


def hackalendar_mcp(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    params = body["params"]
    if params["name"] == "get_hackathon":
        if params["arguments"]["slug"] == HK_DETAIL:
            return json_file("hackalendar/get.json")(request)
        slug = params["arguments"]["slug"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {"type": "text", "text": f'No published event with slug "{slug}".'}
                    ],
                    "isError": True,
                },
            },
        )
    return json_file(f"hackalendar/search_{params['arguments']['offset']}.json")(request)


async def test_hackalendar_pages_mcp_and_maps_precision() -> None:
    router = Router({HK_MCP: hackalendar_mcp})
    run = await run_source({"id": "hackalendar", "adapter": "hackalendar"}, router)
    assert run.errors == []
    search = [json.loads(request.content) for request in router.requests]
    assert search[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_hackathons", "arguments": {"limit": 50, "offset": 0}},
    }
    assert all(request.method == "POST" for request in router.requests)
    assert router.requests[0].headers["accept"] == "application/json, text/event-stream"
    items = by_id(run.items)
    assert len(items) == 4
    assert "hackalendar:eu-vibe-coding-hackathon-from-idea-to-subscription-in-48h-2026" not in items
    health = items[f"hackalendar:{HK_DETAIL}"]
    assert health.starts_at == datetime(2026, 11, 13, 17, tzinfo=UTC)
    assert health.ends_at == datetime(2026, 11, 15, 22, 59, 59, tzinfo=UTC)
    assert health.inferred_fields == ["timezone", "ends_at"]
    assert health.location is not None and health.location.address == "M\u00e9tafore"
    assert health.description_md is not None and health.description_md.startswith("Pour la")
    robotics = items["hackalendar:robotics-ai-hacklab-berlin-2026"]
    assert robotics.prize is not None
    assert (robotics.prize.amount, robotics.prize.currency) == (10000.0, "EUR")
    assert [area.key for area in robotics.areas] == ["ml_ai", "hardware"]
    online = items["hackalendar:threat-intelligence-threat-modelling-hackathon-2026"]
    assert online.format == EventFormat.ONLINE
    assert online.location is None


def tracked(source_id: str, url: str, ends_at: datetime) -> TrackedEvent:
    return TrackedEvent.model_validate({"source_id": source_id, "urls": [url], "ends_at": ends_at})


async def test_hackalendar_revisits_by_slug() -> None:
    def unlisted(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["params"]["name"] == "search_hackathons":
            text = json.dumps({"count": 0, "nextOffset": None, "events": []})
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": text}]},
                },
            )
        return hackalendar_mcp(request)

    ends = datetime(2026, 11, 15, 22, 59, 59, tzinfo=UTC)
    run = await run_source(
        {"id": "hackalendar", "adapter": "hackalendar"},
        Router({HK_MCP: unlisted}),
        revisits=[
            tracked(f"hackalendar:{HK_DETAIL}", HK_MCP, ends),
            tracked("hackalendar:removed-hack-2026", HK_MCP, ends),
        ],
    )
    assert run.errors == []
    assert run.checks == {f"hackalendar:{HK_DETAIL}": True, "hackalendar:removed-hack-2026": False}
    assert [item.source.source_id for item in run.items] == [f"hackalendar:{HK_DETAIL}"]
    assert run.items[0].description_md is not None


KAGGLE_API = "https://api.kaggle.com/v1/competitions.CompetitionApiService/ListCompetitions"
KAGGLE_INTERNAL = "https://www.kaggle.com/api/i/competitions.CompetitionService/ListCompetitions"


def kaggle_router() -> Router:
    def official(request: httpx.Request) -> httpx.Response:
        group = json.loads(request.content)["group"]
        name = "general" if group == "COMPETITION_LIST_TAB_GENERAL" else "community"
        return json_file(f"kaggle/{name}.json")(request)

    def internal(request: httpx.Request) -> httpx.Response:
        page = 2 if json.loads(request.content).get("pageToken") else 1
        return json_file(f"kaggle/internal_{page}.json")(request)

    return Router({KAGGLE_API: official, KAGGLE_INTERNAL: internal})


async def test_kaggle_official_api_with_token() -> None:
    router = kaggle_router()
    run = await run_source(
        {"id": "kaggle", "adapter": "kaggle"}, router, env={"KAGGLE_API_TOKEN": "KGAT_fake"}
    )
    assert run.errors == []
    assert [request.headers["authorization"] for request in router.requests] == [
        "Bearer KGAT_fake"
    ] * 2
    assert json.loads(router.requests[1].content) == {
        "group": "COMPETITION_LIST_TAB_COMMUNITY",
        "sortBy": "COMPETITION_SORT_BY_PRIZE",
        "pageSize": 20,
        "page": 1,
    }
    items = by_id(run.items)
    assert set(items) == {
        "kaggle:178013",
        "kaggle:149921",
        "kaggle:166661",
        "kaggle:157303",
        "kaggle:160984",
    }
    gemma = items["kaggle:149921"]
    assert gemma.kind == EventKind.CONTEST
    assert gemma.format == EventFormat.ONLINE
    assert gemma.prize is not None and (gemma.prize.amount, gemma.prize.currency) == (
        75000.0,
        "USD",
    )
    assert gemma.registration_deadline == datetime(2026, 11, 25, 23, 59, tzinfo=UTC)
    assert gemma.ends_at == datetime(2026, 12, 2, 23, 59, tzinfo=UTC)
    assert gemma.team_size_max == 5
    assert gemma.areas[0].key == "ml_ai"
    assert str(gemma.website_url) == "https://www.kaggle.com/competitions/gemma-4-developer-agent"
    assert items["kaggle:178013"].registration_deadline == items["kaggle:178013"].ends_at


async def test_kaggle_without_token_warns_and_uses_internal_api(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router = kaggle_router()
    with caplog.at_level(logging.WARNING, logger="tests.parser"):
        run = await run_source({"id": "kaggle", "adapter": "kaggle"}, router)
    assert run.errors == []
    assert "KAGGLE_API_TOKEN is not set" in caplog.text
    assert [str(request.url) for request in router.requests] == [KAGGLE_INTERNAL] * 2
    assert "authorization" not in router.requests[0].headers
    assert json.loads(router.requests[1].content)["pageToken"] == "100"
    assert set(by_id(run.items)) == {"kaggle:178013", "kaggle:149921", "kaggle:166661"}


CT_RAW = "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences"
CT_TREE = "https://api.github.com/repos/tech-conferences/conference-data/git/trees/main"
CT_FILES = ("2026/general", "2026/devops", "2026/security", "2027/general")


def confs_router(*, tree: bool = True) -> Router:
    routes = {
        f"{CT_RAW}/{name}.json": respond(f"confs_tech/{name.replace('/', '_')}.json", "text/plain")
        for name in CT_FILES
    }
    if tree:
        routes[CT_TREE] = json_file("confs_tech/tree.json")
    return Router(routes)


async def test_confs_tech_merges_topics_and_maps_formats() -> None:
    router = confs_router()
    run = await run_source({"id": "confs-tech", "adapter": "confs_tech"}, router)
    assert run.errors == []
    assert len(router.requests) == 5
    items = by_id(run.items)
    assert set(items) == {
        "confs-tech:api-conference-berlin@2026-11-16",
        "confs-tech:it-tage@2026-12-07",
        "confs-tech:heapcon@2026-11-05",
        "confs-tech:nextgen-intelligence-conference-data-cloud-ai@2026-11-16",
        "confs-tech:edtech-conference@2026-11-26",
        "confs-tech:voxxed-days-zurich@2027-03-09",
        "confs-tech:inted@2027-03-09",
    }
    api = items["confs-tech:api-conference-berlin@2026-11-16"]
    assert api.kind == EventKind.CONFERENCE
    assert api.format == EventFormat.HYBRID
    assert api.themes == ["devops", "security"]
    assert api.starts_at == datetime(2026, 11, 15, 23, tzinfo=UTC)
    heapcon = items["confs-tech:heapcon@2026-11-05"]
    assert heapcon.location is not None
    assert (heapcon.location.country, heapcon.location.city) == ("RS", "Belgrade")
    assert heapcon.language == ["en"]
    london = items["confs-tech:nextgen-intelligence-conference-data-cloud-ai@2026-11-16"]
    assert london.location is not None and london.location.country == "GB"
    assert items["confs-tech:edtech-conference@2026-11-26"].format == EventFormat.OFFLINE


async def test_confs_tech_falls_back_to_topic_list_and_keeps_online() -> None:
    router = confs_router(tree=False)
    run = await run_source(
        {"id": "confs-tech", "adapter": "confs_tech", "topics": ["general", "devops", "rust"]},
        router,
        now=datetime(2026, 5, 1, 12, tzinfo=UTC),
    )
    assert run.errors == []
    assert len(router.requests) == 1 + 3 * 2
    online = by_id(run.items)["confs-tech:ai-sre-summit@2026-05-12"]
    assert online.format == EventFormat.ONLINE
    assert online.location is None
    assert online.timezone is None
    assert online.themes == ["general", "devops"]


DEVPOST_API = "https://devpost.com/api/hackathons"


def devpost_router() -> Router:
    def listing(request: httpx.Request) -> httpx.Response:
        return json_file(f"devpost/page_{request.url.params['page']}.json")(request)

    return Router(
        {
            DEVPOST_API: listing,
            "https://hacksussex-gamejam-2026.devpost.com/": respond("devpost/hacksussex.html"),
            "https://devfest-ai-hackathon.devpost.com/": respond("devpost/devfest.html"),
            "https://dtit-hackathon-2026.devpost.com/": respond("devpost/kosice.html"),
        }
    )


async def test_devpost_revisits_ended_hackathons_by_search() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == "HackSussex GameJam 2026":
            return json_file("devpost/page_1.json")(request)
        return httpx.Response(200, json={"hackathons": [], "meta": {"total_count": 0}})

    router = devpost_router()
    router.routes[DEVPOST_API] = api
    sussex = "https://hacksussex-gamejam-2026.devpost.com/"
    ends = datetime(2026, 12, 7, tzinfo=UTC)
    run = await run_source(
        {"id": "devpost", "adapter": "devpost"},
        router,
        revisits=[
            tracked("devpost:31655", sussex, ends),
            tracked("devpost:99999", "https://gone.devpost.com/", ends),
        ],
    )
    assert run.errors == []
    assert run.checks == {"devpost:31655": True, "devpost:99999": False}
    item = run.items[0]
    assert item.source.source_id == "devpost:31655"
    assert item.confidence == 0.8
    assert item.location is not None and item.location.country == "GB"


async def test_devpost_reads_team_size_from_requirements() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == "IceHacks":
            return json_file("devpost/search_icehacks.json")(request)
        return httpx.Response(200, json={"hackathons": [], "meta": {"total_count": 0}})

    page = "https://icehacks.devpost.com/"
    router = Router({DEVPOST_API: api, page: respond("devpost/icehacks.html")})
    run = await run_source(
        {"id": "devpost", "adapter": "devpost"},
        router,
        revisits=[tracked("devpost:31495", page, datetime(2026, 11, 15, 23, tzinfo=UTC))],
    )
    assert run.errors == []
    [item] = run.items
    assert (item.team_size_min, item.team_size_max) == (1, 4)
    assert item.description_md is not None
    assert item.description_md.startswith("IceHacks doesn't award any cash prizes.")
    assert "team_size_max" not in item.inferred_fields


async def test_devpost_listing_and_site_json_ld() -> None:
    router = devpost_router()
    run = await run_source({"id": "devpost", "adapter": "devpost", "per_page": 4}, router)
    assert run.errors == []
    first = router.requests[0].url.params
    assert first.get_list("status[]") == ["open", "upcoming"]
    assert (first["per_page"], first["page"]) == ("4", "1")
    fetched = {request.url.host for request in router.requests}
    assert "haven-seattle.devpost.com" not in fetched
    items = by_id(run.items)
    assert set(items) == {"devpost:31667", "devpost:31655", "devpost:30880", "devpost:31602"}
    kiroween = items["devpost:31667"]
    assert kiroween.format == EventFormat.ONLINE
    assert kiroween.confidence == 0.7
    assert kiroween.starts_at == datetime(2026, 10, 30, tzinfo=UTC)
    assert kiroween.ends_at == datetime(2026, 12, 4, 23, 59, 59, tzinfo=UTC)
    assert kiroween.registration_deadline == kiroween.ends_at
    assert kiroween.prize is not None and kiroween.prize.currency == "USD"
    assert kiroween.areas[0].key == "ml_ai"
    sussex = items["devpost:31655"]
    assert sussex.location is not None
    assert (sussex.location.country, sussex.location.address) == ("GB", "Falmer, Brighton, UK")
    assert sussex.starts_at == datetime(2026, 12, 5, 11, tzinfo=UTC)
    assert sussex.registration_deadline is None
    assert sussex.confidence == 0.8
    munster = items["devpost:30880"]
    assert munster.location is not None and munster.location.country == "DE"
    kosice = items["devpost:31602"]
    assert kosice.location is not None and kosice.location.country == "SK"
    assert kosice.prize is not None and (kosice.prize.amount, kosice.prize.currency) == (
        10000.0,
        "EUR",
    )
