import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
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
    is_midnight,
    map_areas,
    parse_datetime,
    slug_terms,
    summary_of,
    text_terms,
    web_url,
    zone_for,
)

_BUNDLE = re.compile(r"/assets/index-[A-Za-z0-9_-]+\.js")
_API = re.compile(r"https://[a-z0-9]+\.supabase\.co")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

KINDS: dict[str, tuple[EventKind, float]] = {
    "hackathon": (EventKind.HACKATHON, 0.85),
    "gamejam": (EventKind.CONTEST, 0.8),
    "competition": (EventKind.CONTEST, 0.6),
    "challenge": (EventKind.CONTEST, 0.6),
}
FORMATS = {
    "onsite": EventFormat.OFFLINE,
    "online": EventFormat.ONLINE,
    "hybrid": EventFormat.HYBRID,
}
LEVELS = {
    "student": "Students only",
    "school": "School students only",
    "professional": "Professionals",
}


class HackathonHubOptions(Contract):
    site: Annotated[str, Field(pattern=r"^https?://")] = "https://hackathonhub.eu"
    api_url: Annotated[str, Field(pattern=r"^https://")] | None = None
    key_env: str = "HACKATHONHUB_ANON_KEY"
    view: str = "events_public"
    page_size: Annotated[int, Field(ge=1, le=1000)] = 1000
    max_pages: Annotated[int, Field(ge=1)] = 20


def boundary(value: Any, zone: str | None, *, end: bool) -> tuple[datetime | None, bool]:
    parsed = parse_datetime(value)
    if parsed is None:
        return None, False
    day = parsed.date()
    if is_midnight(parsed):
        return (day_end(day, zone) if end else day_start(day, zone)), True
    if end and (parsed.hour, parsed.minute) == (23, 59):
        return day_end(day, zone), True
    return parsed, False


@adapter
class HackathonHub(Adapter[HackathonHubOptions]):
    name = "hackathonhub"
    options_model = HackathonHubOptions

    async def credentials(self, ctx: SourceContext) -> tuple[str, str]:
        api, key = self.options.api_url, ctx.env.get(self.options.key_env) or None
        if api is not None and key is not None:
            return api, key
        home = await ctx.get(f"{self.options.site.rstrip('/')}/")
        script = _BUNDLE.search(home.text())
        if script is None:
            raise FetchError(home.url, "hackathonhub bundle not found")
        bundle = (await ctx.get(f"{self.options.site.rstrip('/')}{script[0]}")).text()
        found_api, found_key = _API.search(bundle), _JWT.search(bundle)
        api = api or (found_api[0] if found_api else None)
        key = key or (found_key[0] if found_key else None)
        if api is None or key is None:
            raise FetchError(home.url, "hackathonhub API credentials not found")
        return api, key

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        api, key = await self.credentials(ctx)
        endpoint = f"{api}/rest/v1/{self.options.view}"
        size = self.options.page_size
        for page in range(self.options.max_pages):
            offset = page * size
            response = await ctx.get(
                endpoint,
                params={
                    "select": "*",
                    "end_date": f"gte.{ctx.cutoff.date().isoformat()}",
                    "order": "start_date.asc,id.asc",
                },
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Range-Unit": "items",
                    "Range": f"{offset}-{offset + size - 1}",
                },
                accept=frozenset({200, 206}),
            )
            rows = response.json()
            if not isinstance(rows, list):
                raise FetchError(response.url, "hackathonhub: expected a JSON array")
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    yield Candidate(key=str(row["id"]), url=endpoint, payload=row)
            if len(rows) < size:
                return

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        row: dict[str, Any] = candidate.payload
        country = country_code(str(row.get("country") or "").split(",")[0])
        fmt = FORMATS.get(str(row.get("location_type")))
        if not ctx.in_scope(country, fmt):
            return []
        zone = zone_for(country)
        starts_at, start_inferred = boundary(row.get("start_date"), zone, end=False)
        ends_at, end_inferred = boundary(
            row.get("end_date") or row.get("start_date"), zone, end=True
        )
        if starts_at is None or ends_at is None or ends_at < ctx.cutoff:
            return []
        if ends_at < starts_at:
            ends_at = day_end(starts_at.date(), zone) if zone else starts_at + timedelta(hours=1)
        kind, confidence = KINDS.get(str(row.get("type")), (EventKind.HACKATHON, 0.6))
        title = clean_text(row.get("title_en") or row.get("title"))
        if title is None:
            return []
        description = clean_text(row.get("description_en") or row.get("description"))
        tags = [str(tag) for tag in row.get("tags") or [] if tag]
        deadline = parse_datetime(row.get("application_deadline"))
        amount = row.get("prize_money")
        currency = str(row.get("prize_money_currency") or "").upper()
        team_max = row.get("max_team_size")
        language = str(row.get("language") or "").lower()
        inferred = [
            name
            for name, flag in (
                ("timezone", zone is not None),
                ("starts_at", start_inferred),
                ("ends_at", end_inferred),
            )
            if flag
        ]
        slug = row.get("slug")
        site = self.options.site.rstrip("/")
        website = f"{site}/events/{slug}" if slug else None
        fields: dict[str, Any] = {
            "status": "cancelled" if row.get("status") == "cancelled" else "announced",
            "kind": kind,
            "title": title,
            "summary": summary_of(description),
            "description_md": description,
            "organizer": clean_text(row.get("organizer_name")),
            "website_url": website,
            "registration_url": web_url(row.get("url")),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "registration_deadline": deadline if deadline and deadline < starts_at else None,
            "timezone": zone,
            "format": fmt,
            "location": {
                key: value
                for key, value in (
                    ("country", country),
                    ("city", city_name(row.get("city"))),
                    ("address", clean_text(row.get("address"))),
                )
                if value
            }
            or None,
            "language": [language] if re.fullmatch(r"[a-z]{2}", language) else [],
            "eligibility": LEVELS.get(str(row.get("level"))),
            "themes": tags[:10],
            "areas": map_areas(
                [*slug_terms(tags), *text_terms(title)],
                ctx.areas,
                primary="gamedev" if row.get("type") == "gamejam" else None,
            ),
            "team_size_max": team_max if isinstance(team_max, int) and team_max >= 1 else None,
            "prize": (
                {
                    "amount": float(amount),
                    "currency": currency if re.fullmatch(r"[A-Z]{3}", currency) else None,
                }
                if isinstance(amount, (int, float)) and amount > 0
                else None
            ),
            "confidence": ctx.confidence(confidence),
            "inferred_fields": inferred,
        }
        urls = [url for url in (website, fields["registration_url"]) if url]
        return [self.build(ctx, candidate.key, urls, fields)]
