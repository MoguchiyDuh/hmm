import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser import jsonld
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import FetchError
from hmm.parser.normalize import (
    MONTHS,
    as_dict,
    clean_text,
    clip,
    country_in_text,
    day_end,
    day_start,
    html_to_text,
    map_areas,
    money,
    text_terms,
    web_url,
)

ONLINE_PLACES = frozenset({"online", "virtual", "remote"})
_DATES = re.compile(
    r"^(?P<m1>[A-Za-z]{3})\s+(?P<d1>\d{1,2})(?:,\s*(?P<y1>\d{4}))?"
    r"(?:\s*-\s*(?:(?P<m2>[A-Za-z]{3})\s+)?(?P<d2>\d{1,2}))?,\s*(?P<y2>\d{4})$"
)
_TEST_TITLE = re.compile(r"^(?:sandbox\s+)?(?:hacks?\s+)?test(?:\s+\d+)?$", re.IGNORECASE)
_TRUNCATED = re.compile(r"\s*(?:\.\.\.|…)$")


class DevpostOptions(Contract):
    api: Annotated[str, Field(pattern=r"^https://")] = "https://devpost.com/api/hackathons"
    statuses: Annotated[tuple[str, ...], Field(min_length=1)] = ("open", "upcoming")
    per_page: Annotated[int, Field(ge=1, le=40)] = 40
    max_pages: Annotated[int, Field(ge=1)] = 10
    details: bool = True
    rate_key: str = "devpost.com"


def listing_dates(value: Any) -> tuple[date, date] | None:
    match = _DATES.match(clean_text(str(value or "")) or "")
    if match is None:
        return None
    first_month = MONTHS.get(match["m1"].casefold())
    last_month = MONTHS.get((match["m2"] or match["m1"]).casefold())
    end_year = int(match["y2"])
    start_year = int(match["y1"] or end_year)
    if first_month is None or last_month is None:
        return None
    try:
        start = date(start_year, first_month, int(match["d1"]))
        end = date(end_year, last_month, int(match["d2"] or match["d1"]))
    except ValueError:
        return None
    if end < start and match["y1"] is None:
        start = start.replace(year=end_year - 1)
    return (start, end) if end >= start else None


def listing_format(place: str) -> EventFormat:
    lowered = place.casefold()
    if lowered in ONLINE_PLACES:
        return EventFormat.ONLINE
    if "online" in lowered or "virtual" in lowered:
        return EventFormat.HYBRID
    return EventFormat.OFFLINE


def pool(node: dict[str, Any]) -> tuple[float | None, str | None]:
    for item in node.get("additionalProperty") or []:
        if not isinstance(item, dict) or "prize" not in str(item.get("name") or "").casefold():
            continue
        unit = str(item.get("unitText") or "").upper()
        try:
            amount = float(str(item.get("value")).replace(",", ""))
        except ValueError:
            continue
        if amount > 0 and re.fullmatch(r"[A-Z]{3}", unit):
            return amount, unit
    return None, None


@adapter
class Devpost(Adapter[DevpostOptions]):
    name = "devpost"
    options_model = DevpostOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        seen: set[int] = set()
        for page in range(1, self.options.max_pages + 1):
            response = await ctx.get(
                self.options.api,
                params={
                    "status[]": self.options.statuses,
                    "per_page": self.options.per_page,
                    "page": page,
                },
                rate_key=self.options.rate_key,
            )
            data = response.json()
            rows = data.get("hackathons") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise FetchError(response.url, "devpost: expected a hackathons list")
            for row in rows:
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("id"), int)
                    and row["id"] not in seen
                ):
                    seen.add(row["id"])
                    url = web_url(row.get("url")) or response.url
                    yield Candidate(key=str(row["id"]), url=url, payload=row)
            meta = as_dict(data.get("meta"))
            total = meta.get("total_count")
            if not rows or (isinstance(total, int) and page * self.options.per_page >= total):
                return

    async def detail(self, ctx: SourceContext, url: str) -> dict[str, Any] | None:
        try:
            page = await ctx.get(url, rate_key=self.options.rate_key)
        except FetchError as error:
            ctx.log.info("parser %s: %s detail failed: %s", ctx.source.id, url, error)
            return None
        nodes, index = jsonld.events(page.text())
        for node in nodes:
            found = jsonld.fields(node, index, url)
            if found is not None:
                amount, currency = pool(node)
                return {**found, "pool": (amount, currency)}
        return None

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        row: dict[str, Any] = candidate.payload
        title = clean_text(row.get("title"))
        if title is None or _TEST_TITLE.match(title):
            return []
        displayed = row.get("displayed_location")
        place = clean_text(displayed.get("location")) if isinstance(displayed, dict) else None
        listed_format = listing_format(place or "online")
        listed_country = country_in_text(place) if place else None
        settled = listed_format == EventFormat.ONLINE or (
            listed_format == EventFormat.OFFLINE and listed_country is not None
        )
        if settled and not ctx.in_scope(listed_country, listed_format):
            return []
        found = await self.detail(ctx, candidate.url) if self.options.details else None
        if found is not None:
            fmt = found["format"] or listed_format
            location = found["location"] if fmt != EventFormat.ONLINE else None
            if location and str(location.get("address") or "").startswith(f"{title}, "):
                location = {**location, "address": location["address"][len(title) + 2 :]}
            country = location.get("country") if location else None
            starts_at, ends_at = found["starts_at"], found["ends_at"]
            inferred = found["inferred_fields"]
            zone = found["timezone"] if fmt != EventFormat.ONLINE else None
            amount, currency = found["pool"]
            summary = clean_text(_TRUNCATED.sub("", found["summary"] or "")) or None
            confidence = 0.8
        else:
            window = listing_dates(row.get("submission_period_dates"))
            if window is None:
                return []
            fmt, country, zone = listed_format, listed_country, None
            location = {"country": country, "address": place} if country else None
            starts_at, ends_at = day_start(window[0], None), day_end(window[1], None)
            inferred = ["starts_at", "ends_at"]
            amount, currency, summary = None, None, None
            confidence = 0.7
        if not ctx.in_scope(country, fmt) or ends_at < ctx.cutoff:
            return []
        if amount is None:
            amount, currency = money(html_to_text(str(row.get("prize_amount") or "")))
        themes = [
            str(item["name"])
            for item in row.get("themes") or []
            if isinstance(item, dict) and item.get("name")
        ]
        online = fmt == EventFormat.ONLINE
        fields: dict[str, Any] = {
            "status": found["status"] if found is not None else "announced",
            "kind": EventKind.HACKATHON,
            "title": title,
            "summary": clip(summary, 500),
            "organizer": clean_text(row.get("organization_name"))
            or (found["organizer"] if found is not None else None),
            "website_url": candidate.url,
            "registration_url": (found["registration_url"] if found is not None else None)
            or candidate.url,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "registration_deadline": ends_at if online else None,
            "timezone": zone,
            "format": fmt,
            "location": location,
            "eligibility": "Invite only" if row.get("invite_only") is True else None,
            "themes": themes[:10],
            "areas": map_areas(text_terms(*themes, title), ctx.areas),
            "prize": {"amount": amount, "currency": currency} if amount else None,
            "confidence": ctx.confidence(confidence),
            "inferred_fields": [*inferred, *(["registration_deadline"] if online else [])],
        }
        return [self.build(ctx, candidate.key, [candidate.url], fields)]
