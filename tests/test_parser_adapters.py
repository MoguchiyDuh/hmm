from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from parser_support import Router, json_file, respond, run_source

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
