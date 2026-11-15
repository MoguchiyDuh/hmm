import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import JsonValue

from hmm.catalog.import_schema import EventImport, Location, TrackedEvent
from hmm.catalog.imports import normalize_title
from hmm.common import EventFormat
from hmm.parser.base import EMPTY, Adapter, SourceContext, create_adapter
from hmm.parser.config import SourceConfig
from hmm.parser.fetch import Fetcher, FetchError
from hmm.parser.normalize import EUROPE

MAX_ERRORS_PER_SOURCE = 20
GONE_STATUSES = frozenset({404, 410})
TITLE_PARTS = re.compile(r"\s*\|\s*|\s+[-\u2013\u2014]\s+|:\s+")
MIN_PART_WORDS = 2
DEDUPE_WINDOW = timedelta(days=1)
MERGED_FIELDS: tuple[str, ...] = (
    "summary",
    "description_md",
    "organizer",
    "website_url",
    "registration_url",
    "registration_opens_at",
    "registration_deadline",
    "timezone",
    "format",
    "language",
    "eligibility",
    "themes",
    "areas",
    "prize",
)


@dataclass(slots=True)
class SourceRun:
    source: SourceConfig
    started_at: datetime
    finished_at: datetime | None = None
    candidates: int = 0
    items: list[EventImport] = field(default_factory=list)
    errors: list[dict[str, JsonValue]] = field(default_factory=list)
    dropped: int = 0
    seen: set[str] = field(default_factory=set)
    checks: dict[str, bool] = field(default_factory=dict)

    def error(self, stage: str, message: str, key: str | None = None) -> None:
        if len(self.errors) < MAX_ERRORS_PER_SOURCE:
            entry: dict[str, JsonValue] = {
                "source": self.source.id,
                "stage": stage,
                "error": message[:500],
            }
            if key is not None:
                entry["candidate"] = key[:200]
            self.errors.append(entry)

    def stats(self) -> dict[str, JsonValue]:
        return {
            "candidates": self.candidates,
            "items": len(self.items),
            "dropped": self.dropped,
            "errors": len(self.errors),
            "revisited": len(self.checks),
            "gone": sum(not found for found in self.checks.values()),
        }

    def keep(self, item: EventImport, now: datetime) -> None:
        self.seen.add(item.source.source_id)
        if relevant(self.source, item, now):
            self.items.append(item)
        else:
            self.dropped += 1


def relevant(source: SourceConfig, item: EventImport, now: datetime) -> bool:
    if item.ends_at < now - timedelta(days=source.lookback_days):
        return False
    country = item.location.country if item.location is not None else None
    if country is not None and country in (source.countries or EUROPE):
        return True
    online = item.format in {EventFormat.ONLINE, EventFormat.HYBRID}
    return online and source.include_online


async def collect(
    source: SourceConfig,
    *,
    fetcher: Fetcher,
    now: datetime,
    areas: frozenset[str],
    env: Mapping[str, str],
    log: logging.Logger,
    adapter: Adapter[Any] | None = None,
    revisits: Sequence[TrackedEvent] = (),
) -> SourceRun:
    run = SourceRun(source=source, started_at=now)
    ctx = SourceContext(source=source, fetcher=fetcher, now=now, areas=areas, env=env, log=log)
    try:
        instance = adapter or create_adapter(source)
        for message in instance.warnings(ctx):
            log.warning("parser %s: %s", source.id, message)
        async with asyncio.timeout(source.budget.total_seconds()):
            async for candidate in instance.discover(ctx):
                run.candidates += 1
                try:
                    found = await instance.extract(ctx, candidate)
                except Exception as error:
                    log.warning("parser %s: %s failed: %s", source.id, candidate.key, error)
                    run.error("extract", f"{type(error).__name__}: {error}", candidate.key)
                    continue
                for item in found:
                    run.keep(item, now)
            for tracked in revisits:
                if tracked.source_id not in run.seen:
                    await recheck(run, instance, ctx, tracked)
    except TimeoutError:
        run.error("budget", f"source exceeded its {source.budget} budget")
    except Exception as error:
        log.warning("parser %s: discovery failed: %s", source.id, error)
        run.error("discover", f"{type(error).__name__}: {error}")
    return run


async def recheck(
    run: SourceRun, instance: Adapter[Any], ctx: SourceContext, tracked: TrackedEvent
) -> None:
    try:
        found = await instance.revisit(ctx, tracked)
    except FetchError as error:
        if error.status in GONE_STATUSES:
            run.checks[tracked.source_id] = False
        else:
            run.error("revisit", f"FetchError: {error}", tracked.source_id)
        return
    except Exception as error:
        ctx.log.warning("parser %s: revisit %s failed: %s", ctx.source.id, tracked.source_id, error)
        run.error("revisit", f"{type(error).__name__}: {error}", tracked.source_id)
        return
    if found is None:
        return
    items = [item for item in found if item.source.source_id == tracked.source_id]
    run.checks[tracked.source_id] = bool(items)
    for item in items:
        run.keep(item, ctx.now)


def prefix(item: EventImport) -> str:
    return item.source.source_id.split(":", 1)[0]


def merge(leader: EventImport, follower: EventImport) -> EventImport:
    update: dict[str, Any] = {}
    taken: list[str] = []
    for name in MERGED_FIELDS:
        if getattr(leader, name) in EMPTY and getattr(follower, name) not in EMPTY:
            update[name] = getattr(follower, name)
            taken.append(name)
    if leader.team_size_min is None and leader.team_size_max is None:
        update["team_size_min"] = follower.team_size_min
        update["team_size_max"] = follower.team_size_max
    if follower.location is not None:
        base = leader.location.model_dump() if leader.location is not None else {}
        extra = follower.location.model_dump()
        filled = {key: base.get(key) or extra.get(key) for key in extra}
        if filled != base:
            update["location"] = Location.model_validate(filled)
            taken.append("location")
    inferred = [
        name for name in follower.inferred_fields if name in taken or name.split(".", 1)[0] in taken
    ]
    if inferred:
        update["inferred_fields"] = list(dict.fromkeys([*leader.inferred_fields, *inferred]))
    return leader.model_copy(update=update) if update else leader


def identities(item: EventImport) -> list[tuple[str, str | None]]:
    country = item.location.country if item.location is not None else None
    titles = [normalize_title(item.title)]
    parts = TITLE_PARTS.split(item.title)
    if len(parts) > 1:
        titles.extend(
            text for text in map(normalize_title, parts) if len(text.split()) >= MIN_PART_WORDS
        )
    return [(title, country) for title in dict.fromkeys(titles) if title]


def dedupe(runs: Iterable[SourceRun]) -> tuple[list[EventImport], int]:
    ranked = sorted(
        ((run.source.priority, item) for run in runs for item in run.items),
        key=lambda pair: (-pair[0], -pair[1].confidence),
    )
    leaders: list[EventImport] = []
    buckets: dict[tuple[str, str | None], list[int]] = {}
    merged = 0
    for _, item in ranked:
        keys = identities(item)
        match = next(
            (
                index
                for key in keys
                for index in buckets.get(key, [])
                if prefix(leaders[index]) != prefix(item)
                and abs(leaders[index].starts_at - item.starts_at) <= DEDUPE_WINDOW
            ),
            None,
        )
        if match is None:
            match = len(leaders)
            leaders.append(item)
        else:
            leaders[match] = merge(leaders[match], item)
            merged += 1
        for key in keys:
            bucket = buckets.setdefault(key, [])
            if match not in bucket:
                bucket.append(match)
    leaders.sort(key=lambda item: (item.starts_at, item.source.source_id))
    return leaders, merged


def chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
