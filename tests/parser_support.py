import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from hmm.common import SEED_AREA_KEYS
from hmm.parser.config import source_from_table
from hmm.parser.fetch import Fetcher
from hmm.parser.pipeline import SourceRun, collect

FIXTURES = Path(__file__).parent / "fixtures" / "parser"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

type Handler = Callable[[httpx.Request], httpx.Response]


def fixture(path: str) -> bytes:
    return (FIXTURES / path).read_bytes()


def respond(path: str, content_type: str = "text/html; charset=utf-8") -> Handler:
    return lambda _: httpx.Response(
        200, content=fixture(path), headers={"content-type": content_type}
    )


def json_file(path: str) -> Handler:
    return respond(path, "application/json; charset=utf-8")


class Router:
    def __init__(self, routes: Mapping[str, Handler]) -> None:
        self.routes = dict(routes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        handler = self.routes.get(url)
        if handler is None:
            return httpx.Response(404, content=b"not found")
        return handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def public_resolve(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def no_sleep(_: float) -> None:
    return None


async def run_source(
    table: Mapping[str, Any],
    router: Router,
    *,
    now: datetime = NOW,
    env: Mapping[str, str] | None = None,
) -> SourceRun:
    source = source_from_table(table)
    async with httpx.AsyncClient(transport=router.transport()) as client:
        fetcher = Fetcher(client=client, resolve=public_resolve, sleep=no_sleep)
        return await collect(
            source,
            fetcher=fetcher,
            now=now,
            areas=frozenset(SEED_AREA_KEYS),
            env=env or {},
            log=logging.getLogger("tests.parser"),
        )
