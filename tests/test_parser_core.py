from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from parser_support import NOW, Router, no_sleep, public_resolve, run_source

from hmm.catalog.import_schema import EventImport
from hmm.common import EventFormat
from hmm.parser.base import ADAPTERS, create_adapter, load_adapters
from hmm.parser.config import (
    DEFAULT_SOURCES,
    SourceConfigError,
    load_sources,
    parse_duration,
    parse_rate,
    source_from_table,
)
from hmm.parser.fetch import Fetcher, FetchError
from hmm.parser.normalize import summary_of, team_size
from hmm.parser.pipeline import SourceRun, dedupe, relevant
from hmm.parser.sources.hackathonist import parse_when
from hmm.parser.sources.luma import to_markdown


def fetcher(handler: Any, **kwargs: Any) -> tuple[Fetcher, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(
        client=client, resolve=kwargs.pop("resolve", public_resolve), sleep=no_sleep, **kwargs
    ), client


async def get(target: Fetcher, url: str, max_bytes: int = 1000) -> Any:
    return await target.get(url, spacing=0, request_timeout=5, max_bytes=max_bytes)


@pytest.mark.parametrize(
    "url,addresses",
    [
        ("http://127.0.0.1/", None),
        ("http://[::1]/", None),
        ("http://internal.example/", ["10.0.0.5"]),
        ("http://mixed.example/", ["93.184.216.34", "192.168.1.1"]),
        ("file:///etc/passwd", None),
    ],
)
async def test_fetch_blocks_non_public(url: str, addresses: list[str] | None) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return addresses or []

    calls: list[httpx.Request] = []
    target, client = fetcher(
        lambda request: calls.append(request) or httpx.Response(200), resolve=resolve
    )
    async with client:
        with pytest.raises(FetchError):
            await get(target, url)
    assert calls == []


async def test_fetch_checks_redirect_targets() -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["10.1.1.1"] if host == "evil.example" else ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "http://evil.example/x"})
        return httpx.Response(200, text="secret")

    target, client = fetcher(handler, resolve=resolve)
    async with client:
        with pytest.raises(FetchError, match="non-public"):
            await get(target, "https://start.example/")


async def test_fetch_follows_relative_redirect_and_limits_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/loop":
            return httpx.Response(301, headers={"location": "/loop"})
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    target, client = fetcher(handler)
    async with client:
        fetched = await get(target, "https://site.example/old")
        assert (fetched.url, fetched.text()) == ("https://site.example/new", "ok")
        with pytest.raises(FetchError, match="too many redirects"):
            await get(target, "https://site.example/loop")


async def test_fetch_caps_size() -> None:
    target, client = fetcher(lambda _: httpx.Response(200, content=b"x" * 2000))
    async with client:
        with pytest.raises(FetchError, match="larger than"):
            await get(target, "https://site.example/", max_bytes=1000)
        assert len((await get(target, "https://site.example/", max_bytes=2000)).content) == 2000


async def test_fetch_retries_transient_status() -> None:
    statuses = iter([503, 429, 200])

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, headers={"retry-after": "1"} if status == 429 else {})

    target, client = fetcher(handler)
    async with client:
        assert (await get(target, "https://site.example/")).status == 200
    assert target.requests == 3


async def test_fetch_does_not_retry_client_errors() -> None:
    target, client = fetcher(lambda _: httpx.Response(404))
    async with client:
        with pytest.raises(FetchError) as error:
            await get(target, "https://site.example/")
    assert error.value.status == 404
    assert target.requests == 1


async def test_fetch_spaces_requests_per_host() -> None:
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    target = Fetcher(client=client, resolve=public_resolve, sleep=sleep)
    async with client:
        for _ in range(3):
            await target.get("https://a.example/", spacing=10, request_timeout=5, max_bytes=10)
        await target.get("https://b.example/", spacing=10, request_timeout=5, max_bytes=10)
    assert len(waits) == 2
    assert all(9 < wait <= 20 for wait in waits)


def test_durations_and_rates() -> None:
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_rate("30/min") == 2.0
    assert parse_rate("1/10s") == 10.0
    with pytest.raises(ValueError):
        parse_rate("fast")


def test_default_sources_load_and_build() -> None:
    sources = load_sources(DEFAULT_SOURCES)
    ids = {source.id for source in sources}
    assert {
        "hackathonhub",
        "hackathonist",
        "ctftime",
        "luma-rs",
        "gdg-rs",
        "dev-events",
        "bbicc",
    } <= ids
    ctftime = next(source for source in sources if source.id == "ctftime")
    assert ctftime.spacing == 10.0
    assert ctftime.options == {"window_days": 120}
    for source in sources:
        assert create_adapter(source).name == source.adapter


def test_adapters_register_themselves() -> None:
    assert {"hackathonhub", "hackathonist", "ctftime", "luma", "jsonld_pages"} <= set(
        load_adapters()
    )
    assert ADAPTERS["luma"].name == "luma"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(body)
    return path


def test_source_config_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="duplicate"):
        load_sources(write(tmp_path, '[[source]]\nid="a"\nadapter="ctftime"\n' * 2))
    with pytest.raises(SourceConfigError, match="source #1"):
        load_sources(write(tmp_path, '[[source]]\nid="Bad Id"\nadapter="ctftime"\n'))
    with pytest.raises(SourceConfigError, match="Cannot read"):
        load_sources(tmp_path / "missing.toml")
    with pytest.raises(SourceConfigError, match="Unknown adapter"):
        create_adapter(source_from_table({"id": "x", "adapter": "nope"}))
    with pytest.raises(SourceConfigError, match="Invalid options"):
        create_adapter(source_from_table({"id": "x", "adapter": "ctftime", "typo": 1}))
    with pytest.raises(SourceConfigError, match="Invalid options"):
        create_adapter(source_from_table({"id": "x", "adapter": "luma", "calendars": []}))


def test_missing_secret() -> None:
    source = source_from_table({"id": "k", "adapter": "ctftime", "secret_env": ["TOKEN"]})
    assert source.missing_secrets({}) == ["TOKEN"]
    assert source.missing_secrets({"TOKEN": "x"}) == []


async def test_unknown_adapter_is_a_source_error() -> None:
    run = await run_source({"id": "x", "adapter": "nope"}, Router({}))
    assert run.errors[0]["stage"] == "discover"


def item(source_id: str, title: str, starts: datetime, **fields: Any) -> EventImport:
    return EventImport.model_validate(
        {
            "source": {"source_id": source_id, "urls": [], "fetched_at": NOW},
            "kind": "hackathon",
            "title": title,
            "starts_at": starts,
            "ends_at": starts + timedelta(days=1),
            "confidence": 0.8,
            "extractor_version": "t/1",
            **fields,
        }
    )


def run_of(source_id: str, priority: int, *items: EventImport) -> SourceRun:
    source = source_from_table({"id": source_id, "adapter": "ctftime", "priority": priority})
    return SourceRun(source=source, started_at=NOW, items=list(items))


def test_dedupe_merges_across_sources() -> None:
    start = datetime(2026, 12, 1, 9, tzinfo=UTC)
    leader = item("gdg:1", "DevFest Belgrade 2026", start, location={"country": "RS"})
    follower = item(
        "dev:1",
        "DevFest  Belgrade",
        start + timedelta(hours=12),
        location={"country": "RS", "city": "Belgrade"},
        summary="Google conference",
        timezone="Europe/Belgrade",
        team_size_max=4,
        inferred_fields=["timezone"],
    )
    other = item("dev:2", "DevFest Belgrade", start + timedelta(days=3), location={"country": "RS"})
    same_source = item("gdg:2", "DevFest Belgrade", start, location={"country": "RS"})
    items, merged = dedupe(
        [run_of("dev", 30, follower, other), run_of("gdg", 80, leader, same_source)]
    )
    assert merged == 1
    assert [entry.source.source_id for entry in items] == ["gdg:1", "gdg:2", "dev:2"]
    first = items[0]
    assert first.summary == "Google conference"
    assert first.timezone == "Europe/Belgrade"
    assert first.location is not None and first.location.city == "Belgrade"
    assert first.team_size_max == 4
    assert first.inferred_fields == ["timezone"]


def test_dedupe_keeps_different_countries() -> None:
    start = datetime(2026, 12, 1, 9, tzinfo=UTC)
    items, merged = dedupe(
        [
            run_of("a", 50, item("a:1", "Jam", start, location={"country": "RS"})),
            run_of("b", 50, item("b:1", "Jam", start, location={"country": "HR"})),
        ]
    )
    assert (len(items), merged) == (2, 0)


def test_relevance() -> None:
    source = source_from_table({"id": "s", "adapter": "ctftime", "countries": ["RS"]})
    start = NOW + timedelta(days=3)
    assert relevant(source, item("s:1", "A", start, location={"country": "RS"}), NOW)
    assert not relevant(source, item("s:2", "B", start, location={"country": "HR"}), NOW)
    assert relevant(source, item("s:3", "C", start, format=EventFormat.ONLINE), NOW)
    assert not relevant(source, item("s:4", "D", NOW - timedelta(days=40)), NOW)
    europe = source_from_table({"id": "e", "adapter": "ctftime", "include_online": False})
    assert relevant(europe, item("e:1", "E", start, location={"country": "HR"}), NOW)
    assert not relevant(europe, item("e:2", "F", start, location={"country": "US"}), NOW)
    assert not relevant(europe, item("e:3", "G", start, format=EventFormat.ONLINE), NOW)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("6-7 November 2026", (date(2026, 11, 6), date(2026, 11, 7))),
        ("12th September 2026", (date(2026, 9, 12), date(2026, 9, 12))),
        ("24th April - 29th May 2026", (date(2026, 4, 24), date(2026, 5, 29))),
        ("31 May \u2013 2 June 2024", (date(2024, 5, 31), date(2024, 6, 2))),
        ("28 December 2020 - 4 January 2021", (date(2020, 12, 28), date(2021, 1, 4))),
        ("29 February 2020", (date(2020, 2, 29), date(2020, 2, 29))),
        ("March 2020", None),
        ("", None),
        ("31 February 2021", None),
    ],
)
def test_hackathonist_dates(text: str, expected: tuple[date, date] | None) -> None:
    assert parse_when(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("team-based event: teams of 1 to 4 players", (1, 4)),
        ("Teams of up to 5 participants (school students)", (None, 5)),
        ("Team size: up to 3", (None, 3)),
        ("teams of 3\u20135 players", (3, 5)),
        ("Teams can have 1 to 10 members (6 to 8 recommended)", (1, 10)),
        ("aged 18-26", (None, None)),
    ],
)
def test_team_size(text: str, expected: tuple[int | None, int | None]) -> None:
    assert team_size(text) == expected


def test_prosemirror_to_markdown() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Agenda"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Join "},
                    {"type": "text", "text": "us ", "marks": [{"type": "bold"}]},
                    {
                        "type": "text",
                        "text": "here",
                        "marks": [{"type": "link", "attrs": {"href": "https://x.example/"}}],
                    },
                    {"type": "hard_break"},
                    {"type": "text", "text": "line"},
                ],
            },
            {
                "type": "bullet_list",
                "content": [
                    {
                        "type": "list_item",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                        ],
                    },
                    {
                        "type": "list_item",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                        ],
                    },
                ],
            },
            {
                "type": "ordered_list",
                "content": [
                    {
                        "type": "list_item",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}
                        ],
                    },
                ],
            },
        ],
    }
    assert to_markdown(doc) == (
        "## Agenda\n\nJoin **us** [here](https://x.example/)  \nline\n\n- one\n- two\n\n1. first"
    )
    assert summary_of(to_markdown(doc)) == "Join us here line"
