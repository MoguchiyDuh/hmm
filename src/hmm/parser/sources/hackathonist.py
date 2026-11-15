import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.normalize import (
    CITY_NAMES,
    MONTHS,
    clean_text,
    day_end,
    day_start,
    map_areas,
    text_terms,
    web_url,
    zone_for,
)

_LINK = re.compile(r"\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)")
_MONTH = r"([A-Za-z]+)"
_RANGE_SAME_MONTH = re.compile(rf"^(\d{{1,2}})\s*-\s*(\d{{1,2}}) {_MONTH} (\d{{4}})$")
_RANGE = re.compile(rf"^(\d{{1,2}}) {_MONTH}(?: (\d{{4}}))?\s*-\s*(\d{{1,2}}) {_MONTH} (\d{{4}})$")
_SINGLE = re.compile(rf"^(\d{{1,2}}) {_MONTH} (\d{{4}})$")
_ONLINE = re.compile(r"\b(online|virtual|on-line)\b", re.IGNORECASE)


class HackathonistOptions(Contract):
    base: Annotated[str, Field(pattern=r"^https://")] = (
        "https://raw.githubusercontent.com/zsevic/hackathonist/gh-pages"
    )
    files: Annotated[list[str], Field(min_length=1)] = Field(default_factory=lambda: ["README.md"])
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] = "RS"


@dataclass(frozen=True, slots=True)
class Row:
    title: str
    url: str
    location: str
    when: str


def day(day_text: str, month_text: str, year_text: str) -> date | None:
    month = MONTHS.get(month_text.casefold())
    if month is None:
        return None
    try:
        return date(int(year_text), month, int(day_text))
    except ValueError:
        return None


def parse_when(value: str) -> tuple[date, date] | None:
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value.strip(), flags=re.IGNORECASE)
    text = " ".join(re.sub(r"[\u2013\u2014]", "-", text).split())
    if match := _RANGE_SAME_MONTH.match(text):
        first, last = day(match[1], match[3], match[4]), day(match[2], match[3], match[4])
    elif match := _RANGE.match(text):
        first = day(match[1], match[2], match[3] or match[6])
        last = day(match[4], match[5], match[6])
    elif match := _SINGLE.match(text):
        first = last = day(match[1], match[2], match[3])
    else:
        return None
    if first is None or last is None or last < first:
        return None
    return first, last


def parse_rows(markdown: str) -> list[Row]:
    rows: list[Row] = []
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        link = _LINK.search(cells[0])
        if link is None:
            continue
        rows.append(
            Row(
                title=link["title"].strip(),
                url=link["url"].strip(),
                location=cells[1],
                when=cells[2],
            )
        )
    return rows


def place(location: str) -> tuple[EventFormat | None, str | None, str | None]:
    text = location.strip()
    if not text:
        return None, None, None
    online = _ONLINE.search(text) is not None
    parts = [part.strip() for part in re.split(r"[,/]", text) if part.strip()]
    physical = [part for part in parts if not _ONLINE.fullmatch(part)]
    city = next(
        (CITY_NAMES[part.casefold()] for part in physical if part.casefold() in CITY_NAMES), None
    )
    venue = ", ".join(part for part in physical if part.casefold() not in CITY_NAMES) or None
    if online:
        return (EventFormat.HYBRID if physical else EventFormat.ONLINE), city, venue
    return EventFormat.OFFLINE, city, venue


def href_key(url: str) -> str:
    parts = urlsplit(url.strip())
    normalized = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


@adapter
class Hackathonist(Adapter[HackathonistOptions]):
    name = "hackathonist"
    options_model = HackathonistOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        for name in self.options.files:
            url = f"{self.options.base.rstrip('/')}/{name}"
            page = await ctx.get(url)
            for row in parse_rows(page.text()):
                yield Candidate(key=f"{row.url}|{row.when}", url=url, payload=row)

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        row: Row = candidate.payload
        window = parse_when(row.when)
        website = web_url(row.url)
        title = clean_text(row.title)
        if window is None or website is None or title is None:
            return []
        fmt, city, venue = place(row.location)
        country = self.options.country
        if not ctx.in_scope(country, fmt):
            return []
        zone = zone_for(country)
        first, last = window
        ends_at = day_end(last, zone)
        if ends_at < ctx.cutoff:
            return []
        location: dict[str, Any] = {
            key: value
            for key, value in (("country", country), ("city", city), ("address", venue))
            if value
        }
        fields: dict[str, Any] = {
            "kind": EventKind.HACKATHON,
            "title": title,
            "website_url": website,
            "starts_at": day_start(first, zone),
            "ends_at": ends_at,
            "timezone": zone,
            "format": fmt,
            "location": location,
            "areas": map_areas(text_terms(title), ctx.areas),
            "confidence": ctx.confidence(0.8),
            "inferred_fields": ["timezone", "starts_at", "ends_at", "location.country"],
        }
        local = f"{href_key(row.url)}-{first:%Y%m%d}"
        return [self.build(ctx, local, [candidate.url, website], fields)]
