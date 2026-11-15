import asyncio
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "hmm-parser/0.1 (+https://github.com/)"
MAX_REDIRECTS = 5
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ROBOTS_MAX_BYTES = 512 * 1024
ROBOTS_TIMEOUT = 10.0
MAX_CRAWL_DELAY = 60.0

type Resolver = Callable[[str, int], Awaitable[list[str]]]
type Sleeper = Callable[[float], Awaitable[None]]
type Params = Mapping[str, str | int | Sequence[str | int]]


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason
        self.status = status


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    def text(self) -> str:
        match = re.search(r"charset=([\w.-]+)", self.headers.get("content-type", ""), re.I)
        try:
            return self.content.decode(match[1] if match else "utf-8", errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except ValueError as error:
            raise FetchError(self.url, f"invalid JSON ({error})", self.status) from error


@dataclass(slots=True)
class Fetcher:
    client: httpx.AsyncClient
    resolve: Resolver = resolve_host
    sleep: Sleeper = asyncio.sleep
    user_agent: str = USER_AGENT
    retries: int = 2
    robots: bool = True
    requests: int = 0
    _next: dict[str, float] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _delays: dict[str, float] = field(default_factory=dict)

    async def get(
        self,
        url: str,
        *,
        spacing: float,
        request_timeout: float,
        max_bytes: int,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        accept: frozenset[int] = frozenset({200}),
        rate_key: str | None = None,
    ) -> Fetched:
        return await self.request(
            "GET",
            url,
            spacing=spacing,
            request_timeout=request_timeout,
            max_bytes=max_bytes,
            params=params,
            headers=headers,
            accept=accept,
            rate_key=rate_key,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        spacing: float,
        request_timeout: float,
        max_bytes: int,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        accept: frozenset[int] = frozenset({200}),
        rate_key: str | None = None,
    ) -> Fetched:
        attempt = 0
        while True:
            try:
                fetched = await self._follow(
                    method,
                    url,
                    spacing=spacing,
                    request_timeout=request_timeout,
                    max_bytes=max_bytes,
                    params=params,
                    headers=headers,
                    body=body,
                    rate_key=rate_key,
                )
            except httpx.TransportError as error:
                if attempt >= self.retries:
                    raise FetchError(url, f"transport error ({error})") from error
            else:
                if fetched.status in accept:
                    return fetched
                if fetched.status not in RETRY_STATUSES or attempt >= self.retries:
                    raise FetchError(fetched.url, f"HTTP {fetched.status}", fetched.status)
                delay = retry_after(fetched.headers)
                if delay is not None:
                    attempt += 1
                    await self.sleep(delay)
                    continue
            attempt += 1
            await self.sleep(max(spacing, 1.0) * 2**attempt)

    async def _follow(
        self,
        method: str,
        url: str,
        *,
        spacing: float,
        request_timeout: float,
        max_bytes: int,
        params: Params | None,
        headers: Mapping[str, str] | None,
        body: Any,
        rate_key: str | None,
    ) -> Fetched:
        current = url
        query = params
        follow = method == "GET"
        for _ in range(MAX_REDIRECTS + 1):
            await self._guard(current)
            delay = await self.crawl_delay(current)
            await self._wait(rate_key or urlsplit(current).hostname or "", max(spacing, delay))
            request = self.client.build_request(
                method,
                current,
                params=query,
                headers={"User-Agent": self.user_agent, **(headers or {})},
                json=body,
                timeout=request_timeout,
            )
            self.requests += 1
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                if follow and response.is_redirect and "location" in response.headers:
                    current = urljoin(str(response.url), response.headers["location"])
                    query = None
                    continue
                content = await read_capped(response, max_bytes, str(response.url))
                return Fetched(
                    url=str(response.url),
                    status=response.status_code,
                    headers=response.headers,
                    content=content,
                )
            finally:
                await response.aclose()
        raise FetchError(url, "too many redirects")

    async def _guard(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise FetchError(url, "unsupported URL")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                addresses = await self.resolve(host, port)
            except OSError as error:
                raise FetchError(url, f"DNS failure ({error})") from error
        else:
            addresses = [str(literal)]
        if not addresses or not all(ipaddress.ip_address(value).is_global for value in addresses):
            raise FetchError(url, "blocked non-public address")

    async def crawl_delay(self, url: str) -> float:
        if not self.robots:
            return 0.0
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._delays:
            async with self._locks.setdefault(f"robots {origin}", asyncio.Lock()):
                if origin not in self._delays:
                    self._delays[origin] = await self._read_robots(origin)
        return self._delays[origin]

    async def _read_robots(self, origin: str) -> float:
        url = f"{origin}/robots.txt"
        try:
            await self._guard(url)
            request = self.client.build_request(
                "GET", url, headers={"User-Agent": self.user_agent}, timeout=ROBOTS_TIMEOUT
            )
            self.requests += 1
            response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.status_code != 200:
                    return 0.0
                content = await read_capped(response, ROBOTS_MAX_BYTES, url)
            finally:
                await response.aclose()
        except (FetchError, httpx.HTTPError):
            return 0.0
        found = crawl_delay(content.decode("utf-8", errors="replace"), self.user_agent)
        return min(found, MAX_CRAWL_DELAY)

    async def _wait(self, host: str, spacing: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            ready = self._next.get(host, now)
            if ready > now:
                await self.sleep(ready - now)
            self._next[host] = max(ready, now) + spacing


async def read_capped(response: httpx.Response, max_bytes: int, url: str) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(url, f"response larger than {max_bytes} bytes", response.status_code)
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(url, f"response larger than {max_bytes} bytes", response.status_code)
        chunks.append(chunk)
    return b"".join(chunks)


def crawl_delay(robots: str, user_agent: str) -> float:
    token = user_agent.split("/", 1)[0].strip().casefold()
    groups: list[tuple[set[str], float | None]] = []
    agents: set[str] = set()
    delay: float | None = None
    rules = False
    for raw in robots.splitlines():
        key, _, value = raw.split("#", 1)[0].partition(":")
        key, value = key.strip().casefold(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, delay))
                agents, delay, rules = set(), None, False
            agents.add(value.casefold())
        elif agents and key:
            rules = True
            if key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
    if agents:
        groups.append((agents, delay))
    for wanted in (lambda name: name != "*" and name in token, lambda name: name == "*"):
        matched = [found for names, found in groups if any(wanted(name) for name in names)]
        if matched:
            return max((found for found in matched if found is not None and found > 0), default=0.0)
    return 0.0


def retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after", "")
    if value.isdigit() and int(value) <= 120:
        return float(value)
    return None
