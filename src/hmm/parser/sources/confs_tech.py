import re
import unicodedata
from collections.abc import AsyncIterator
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import FetchError
from hmm.parser.normalize import (
    city_name,
    clean_text,
    country_code,
    day_end,
    day_start,
    map_areas,
    parse_date,
    slug_terms,
    text_terms,
    web_url,
    zone_for,
)

TOPICS = (
    "accessibility",
    "android",
    "api",
    "clojure",
    "cpp",
    "css",
    "data",
    "devops",
    "dotnet",
    "general",
    "graphql",
    "groovy",
    "haskell",
    "ios",
    "iot",
    "java",
    "javascript",
    "kotlin",
    "leadership",
    "networking",
    "opensource",
    "performance",
    "php",
    "product",
    "python",
    "rust",
    "security",
    "sre",
    "tech-comm",
    "testing",
    "typescript",
    "ux",
)
_FILE = re.compile(r"^conferences/(\d{4})/([a-z0-9-]+)\.json$")


class ConfsTechOptions(Contract):
    repo: Annotated[str, Field(pattern=r"^[\w.-]+/[\w.-]+$")] = "tech-conferences/conference-data"
    branch: Annotated[str, Field(pattern=r"^[\w./-]+$")] = "main"
    raw: Annotated[str, Field(pattern=r"^https://")] = "https://raw.githubusercontent.com"
    api: Annotated[str, Field(pattern=r"^https://")] = "https://api.github.com"
    topics: tuple[str, ...] = TOPICS
    years_ahead: Annotated[int, Field(ge=0, le=3)] = 1


def slug_of(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-") or "event"


@adapter
class ConfsTech(Adapter[ConfsTechOptions]):
    name = "confs_tech"
    options_model = ConfsTechOptions

    async def files(self, ctx: SourceContext, years: range) -> tuple[list[str], bool]:
        tree_url = (
            f"{self.options.api.rstrip('/')}/repos/{self.options.repo}/git/trees/"
            f"{self.options.branch}"
        )
        try:
            tree = (await ctx.get(tree_url, params={"recursive": 1})).json()
        except FetchError as error:
            ctx.log.info("parser %s: git tree unavailable (%s)", ctx.source.id, error)
        else:
            entries = tree.get("tree") if isinstance(tree, dict) else None
            if isinstance(entries, list):
                paths = [
                    entry["path"]
                    for entry in entries
                    if isinstance(entry, dict)
                    and entry.get("type") == "blob"
                    and isinstance(entry.get("path"), str)
                    and (match := _FILE.match(entry["path"])) is not None
                    and int(match[1]) in years
                ]
                return sorted(paths), False
        paths = [
            f"conferences/{year}/{topic}.json" for year in years for topic in self.options.topics
        ]
        return paths, True

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        years = range(ctx.cutoff.year, ctx.now.year + self.options.years_ahead + 1)
        paths, guessed = await self.files(ctx, years)
        base = f"{self.options.raw.rstrip('/')}/{self.options.repo}/{self.options.branch}"
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        oldest = ctx.cutoff.date()
        for path in paths:
            url = f"{base}/{path}"
            response = await ctx.get(url, accept=frozenset({200, 404} if guessed else {200}))
            if response.status == 404:
                continue
            rows = response.json()
            if not isinstance(rows, list):
                raise FetchError(url, "confs.tech: expected a JSON array")
            topic = path.rsplit("/", 1)[-1].removesuffix(".json")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    continue
                start = parse_date(row.get("startDate"))
                end = parse_date(row.get("endDate")) or start
                if start is None or end is None or end < oldest:
                    continue
                key = (row["name"].strip().casefold(), start.isoformat())
                group = groups.setdefault(key, {"row": row, "topics": [], "url": url})
                if topic not in group["topics"]:
                    group["topics"].append(topic)
        for (name, start), group in groups.items():
            yield Candidate(key=f"{slug_of(name)}@{start}", url=group["url"], payload=group)

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        row: dict[str, Any] = candidate.payload["row"]
        topics: list[str] = candidate.payload["topics"]
        city = city_name(clean_text(row.get("city")))
        country = country_code(clean_text(row.get("country")))
        online = row.get("online") is True
        if city or country:
            fmt = EventFormat.HYBRID if online else EventFormat.OFFLINE
        else:
            fmt = EventFormat.ONLINE if online else None
        if not ctx.in_scope(country, fmt):
            return []
        start, end = parse_date(row.get("startDate")), parse_date(row.get("endDate"))
        title = clean_text(row.get("name"))
        if start is None or title is None:
            return []
        end = end if end is not None and end >= start else start
        zone = zone_for(country) if fmt != EventFormat.ONLINE else None
        starts_at, ends_at = day_start(start, zone), day_end(end, zone)
        if ends_at < ctx.cutoff:
            return []
        cfp_url = web_url(row.get("cfpUrl"))
        cfp_end = parse_date(row.get("cfpEndDate"))
        description = (
            f"Call for papers: {cfp_url}" + (f" (closes {cfp_end.isoformat()})" if cfp_end else "")
            if cfp_url
            else None
        )
        languages = [
            code
            for code in (part.strip().lower() for part in str(row.get("locales") or "").split(","))
            if re.fullmatch(r"[a-z]{2}", code)
        ]
        website = web_url(row.get("url"))
        fields: dict[str, Any] = {
            "kind": EventKind.CONFERENCE,
            "title": title,
            "description_md": description,
            "website_url": website,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": zone,
            "format": fmt,
            "location": {
                key: value for key, value in (("country", country), ("city", city)) if value
            }
            or None,
            "language": list(dict.fromkeys(languages)),
            "themes": topics[:10],
            "areas": map_areas([*slug_terms(topics), *text_terms(title)], ctx.areas),
            "confidence": ctx.confidence(0.8),
            "inferred_fields": [*(["timezone"] if zone else []), "starts_at", "ends_at"],
        }
        urls = [url for url in (website, candidate.url) if url]
        return [self.build(ctx, candidate.key, urls, fields)]
