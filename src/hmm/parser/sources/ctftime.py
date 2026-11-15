from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import FetchError
from hmm.parser.normalize import (
    clean_text,
    clip,
    country_in_text,
    epoch,
    map_areas,
    parse_datetime,
    summary_of,
    team_size,
    web_url,
    zone_for,
)


class CtftimeOptions(Contract):
    api: Annotated[str, Field(pattern=r"^https://")] = "https://ctftime.org/api/v1/events/"
    window_days: Annotated[int, Field(ge=1, le=730)] = 120
    limit: Annotated[int, Field(ge=1, le=1000)] = 200


@adapter
class Ctftime(Adapter[CtftimeOptions]):
    name = "ctftime"
    options_model = CtftimeOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        response = await ctx.get(
            self.options.api,
            params={
                "limit": self.options.limit,
                "start": epoch(ctx.cutoff),
                "finish": epoch(ctx.now + timedelta(days=self.options.window_days)),
            },
        )
        events = response.json()
        if not isinstance(events, list):
            raise FetchError(response.url, "ctftime: expected a JSON array")
        if len(events) >= self.options.limit:
            ctx.log.warning("parser %s: hit the %d event limit", ctx.source.id, self.options.limit)
        for event in events:
            if isinstance(event, dict) and isinstance(event.get("id"), int):
                yield Candidate(key=str(event["id"]), url=response.url, payload=event)

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        event: dict[str, Any] = candidate.payload
        where = clean_text(str(event.get("location") or "")) or ""
        onsite = bool(event.get("onsite"))
        if onsite:
            fmt = EventFormat.HYBRID if "online" in where.casefold() else EventFormat.OFFLINE
        else:
            fmt = EventFormat.ONLINE
        country = country_in_text(where) if where else None
        if not ctx.in_scope(country if onsite else None, fmt):
            return []
        starts_at, ends_at = parse_datetime(event.get("start")), parse_datetime(event.get("finish"))
        title = clean_text(event.get("title"))
        if starts_at is None or ends_at is None or title is None or ends_at < ctx.cutoff:
            return []
        description = clean_text(event.get("description"))
        low, high = team_size(description)
        prizes = clip(clean_text(event.get("prizes")), 2000)
        restrictions = clean_text(event.get("restrictions"))
        organizers = [
            str(item["name"]).strip()
            for item in event.get("organizers") or []
            if isinstance(item, dict) and item.get("name")
        ]
        page = web_url(event.get("ctftime_url")) or f"https://ctftime.org/event/{candidate.key}/"
        website = web_url(event.get("url")) or page
        zone = zone_for(country) if onsite else None
        style = clean_text(event.get("format"))
        fields: dict[str, Any] = {
            "kind": EventKind.CONTEST,
            "title": title,
            "summary": summary_of(description),
            "description_md": clip(description, 20000),
            "organizer": ", ".join(organizers) or None,
            "website_url": website,
            "registration_url": page,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": zone,
            "format": fmt,
            "location": (
                {key: value for key, value in (("country", country), ("address", where)) if value}
                if onsite
                else None
            ),
            "eligibility": restrictions if restrictions and restrictions != "Open" else None,
            "themes": ["CTF", *([style] if style else [])],
            "areas": map_areas([], ctx.areas, primary="security"),
            "team_size_min": low,
            "team_size_max": high,
            "prize": {"description": prizes} if prizes else None,
            "confidence": ctx.confidence(0.85),
            "inferred_fields": ["timezone"] if zone else [],
        }
        return [self.build(ctx, candidate.key, [page, website], fields)]
