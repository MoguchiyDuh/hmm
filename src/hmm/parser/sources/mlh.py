import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, time
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import Fetched, FetchError
from hmm.parser.normalize import (
    as_dict,
    city_name,
    clean_text,
    country_code,
    day_end,
    day_start,
    map_areas,
    parse_datetime,
    slug_terms,
    text_terms,
    web_url,
    zone_for,
)

FORMATS = {
    "physical": EventFormat.OFFLINE,
    "hybrid_physical": EventFormat.HYBRID,
    "hybrid": EventFormat.HYBRID,
    "digital": EventFormat.ONLINE,
    "virtual": EventFormat.ONLINE,
    "online": EventFormat.ONLINE,
}
LISTS = (("upcomingEvents", "upcoming_page"), ("pastEvents", "page"))
PLACEHOLDER_START = time(1, 11, 11)
PLACEHOLDER_END = time(23, 59, 59)
_PAGE = re.compile(r"<script[^>]*data-page=\"app\"[^>]*>(.*?)</script>", re.DOTALL)


class MlhOptions(Contract):
    site: Annotated[str, Field(pattern=r"^https://")] = "https://www.mlh.com"
    regions: tuple[str, ...] = ("EMEA",)
    max_pages: Annotated[int, Field(ge=1)] = 10


def season_of(value: datetime) -> int:
    return value.year + 1 if value.month >= 7 else value.year


def inertia_page(response: Fetched) -> dict[str, Any]:
    match = _PAGE.search(response.text())
    if match is None:
        raise FetchError(response.url, "mlh: Inertia page data not found")
    try:
        data = json.loads(match[1])
    except ValueError as error:
        raise FetchError(response.url, f"mlh: invalid page data ({error})") from error
    if not isinstance(data, dict) or not isinstance(data.get("props"), dict):
        raise FetchError(response.url, "mlh: unexpected page data")
    return data


def boundary(value: Any, zone: str | None, *, end: bool) -> tuple[datetime | None, bool]:
    parsed = parse_datetime(value)
    if parsed is None:
        return None, False
    placeholder = PLACEHOLDER_END if end else PLACEHOLDER_START
    if parsed.timetz().replace(tzinfo=None) == placeholder:
        day = parsed.date()
        return (day_end(day, zone) if end else day_start(day, zone)), True
    return parsed, False


@adapter
class Mlh(Adapter[MlhOptions]):
    name = "mlh"
    options_model = MlhOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        site = self.options.site.rstrip("/")
        seen: set[str] = set()
        for season in range(season_of(ctx.cutoff), season_of(ctx.now) + 2):
            url = f"{site}/seasons/{season}/events"
            first = await ctx.get(url, accept=frozenset({200, 404}))
            if first.status == 404:
                continue
            first_page = inertia_page(first)
            for key, param in LISTS:
                data, fetched = first_page, 1
                while True:
                    events = data["props"].get(key) or []
                    for event in events:
                        if isinstance(event, dict) and event.get("id") and event["id"] not in seen:
                            seen.add(event["id"])
                            yield Candidate(key=str(event["id"]), url=url, payload=event)
                    if (
                        key == "pastEvents"
                        and events
                        and all(
                            (ends := parse_datetime(event.get("endsAt"))) is not None
                            and ends < ctx.cutoff
                            for event in events
                            if isinstance(event, dict)
                        )
                    ):
                        break
                    scroll = (data.get("scrollProps") or {}).get(key) or {}
                    following = scroll.get("nextPage")
                    if not isinstance(following, int) or fetched >= self.options.max_pages:
                        break
                    data = inertia_page(await ctx.get(url, params={param: following}))
                    fetched += 1

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        event: dict[str, Any] = candidate.payload
        region = event.get("region")
        if self.options.regions and region is not None and region not in self.options.regions:
            return []
        venue = as_dict(event.get("venueAddress"))
        country = country_code(venue.get("country"))
        fmt = FORMATS.get(str(event.get("formatType")))
        if not ctx.in_scope(country, fmt):
            return []
        zone = zone_for(country)
        starts_at, start_inferred = boundary(event.get("startsAt"), zone, end=False)
        ends_at, end_inferred = boundary(event.get("endsAt"), zone, end=True)
        title = clean_text(event.get("name"))
        if title is None or starts_at is None or ends_at is None or ends_at < ctx.cutoff:
            return []
        if ends_at < starts_at:
            ends_at = day_end(starts_at.date(), zone)
        custom = as_dict(event.get("customFields"))
        focus = [str(item) for item in custom.get("hackathon_focus") or [] if item]
        slug = event.get("slug")
        page = f"{self.options.site.rstrip('/')}/events/{slug}" if slug else candidate.url
        website = web_url(event.get("websiteUrl")) or page
        inferred = [
            name
            for name, flag in (
                ("timezone", zone is not None),
                ("starts_at", start_inferred),
                ("ends_at", end_inferred),
            )
            if flag
        ]
        fields: dict[str, Any] = {
            "kind": EventKind.HACKATHON,
            "title": title,
            "organizer": clean_text(event.get("organizationName")),
            "website_url": website,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": zone,
            "format": fmt,
            "location": None
            if fmt == EventFormat.ONLINE
            else {
                key: value
                for key, value in (
                    ("country", country),
                    ("city", city_name(venue.get("city"))),
                    ("address", clean_text(event.get("location"))),
                )
                if value
            }
            or None,
            "themes": ["MLH", *focus][:10],
            "areas": map_areas([*slug_terms(focus), *text_terms(title)], ctx.areas),
            "confidence": ctx.confidence(0.8),
            "inferred_fields": inferred,
        }
        return [self.build(ctx, candidate.key, [page, website], fields)]
