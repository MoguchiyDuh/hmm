import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport, TrackedEvent
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import Fetched, FetchError
from hmm.parser.normalize import (
    city_name,
    clean_text,
    country_code,
    day_end,
    day_start,
    map_areas,
    money,
    parse_date,
    parse_datetime,
    slug_terms,
    summary_of,
    team_size,
    text_terms,
    web_url,
    zone_for,
    zone_name,
)

MODES = {
    "in_person": EventFormat.OFFLINE,
    "online": EventFormat.ONLINE,
    "hybrid": EventFormat.HYBRID,
}
NOT_A_CITY = frozenset({"online", "location to confirm", "tbc", "tba"})


class HackalendarOptions(Contract):
    endpoint: Annotated[str, Field(pattern=r"^https://")] = "https://hackalendar.com/api/mcp"
    page_size: Annotated[int, Field(ge=1, le=50)] = 50
    max_pages: Annotated[int, Field(ge=1)] = 10
    details: bool = True


def rpc_result(response: Fetched) -> Any:
    if "text/event-stream" in response.headers.get("content-type", ""):
        lines = [
            line.removeprefix("data:").strip()
            for line in response.text().splitlines()
            if line.startswith("data:")
        ]
        if not lines:
            raise FetchError(response.url, "hackalendar: empty event stream")
        envelope = json.loads(lines[-1])
    else:
        envelope = response.json()
    if not isinstance(envelope, dict) or "error" in envelope:
        raise FetchError(response.url, f"hackalendar: RPC error {envelope.get('error')!r}")
    result = envelope.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not content:
        raise FetchError(response.url, "hackalendar: tool call failed")
    block = content[0]
    if not isinstance(block, dict) or not isinstance(block.get("text"), str):
        raise FetchError(response.url, "hackalendar: unexpected tool output")
    if result.get("isError"):
        if block["text"].startswith("No published event"):
            raise FetchError(response.url, "hackalendar: event not found", 404)
        raise FetchError(response.url, f"hackalendar: tool error {block['text'][:200]!r}")
    try:
        return json.loads(block["text"])
    except ValueError as error:
        raise FetchError(response.url, f"hackalendar: invalid tool JSON ({error})") from error


def boundary(
    value: Any, precision: Any, zone: str | None, *, end: bool
) -> tuple[datetime | None, bool]:
    parsed = parse_datetime(value)
    if parsed is not None and precision != "date":
        return parsed, False
    day = parse_date(value)
    if day is None:
        return None, False
    return (day_end(day, zone) if end else day_start(day, zone)), True


@adapter
class Hackalendar(Adapter[HackalendarOptions]):
    name = "hackalendar"
    options_model = HackalendarOptions

    async def call(self, ctx: SourceContext, tool: str, arguments: dict[str, Any]) -> Any:
        response = await ctx.post(
            self.options.endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        return rpc_result(response)

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        offset: Any = 0
        for _ in range(self.options.max_pages):
            data = await self.call(
                ctx, "search_hackathons", {"limit": self.options.page_size, "offset": offset}
            )
            if not isinstance(data, dict):
                raise FetchError(self.options.endpoint, "hackalendar: expected an object")
            for event in data.get("events") or []:
                if isinstance(event, dict) and isinstance(event.get("slug"), str):
                    url = web_url(event.get("url")) or self.options.endpoint
                    yield Candidate(key=event["slug"], url=url, payload=event)
            offset = data.get("nextOffset")
            if not isinstance(offset, int):
                return

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        event: dict[str, Any] = dict(candidate.payload)
        if not ctx.in_scope(
            country_code(event.get("countryCode")), MODES.get(str(event.get("mode")))
        ):
            return []
        if self.options.details:
            try:
                detail = await self.call(ctx, "get_hackathon", {"slug": candidate.key})
            except FetchError as error:
                ctx.log.info("parser %s: %s detail failed: %s", ctx.source.id, candidate.key, error)
            else:
                if isinstance(detail, dict):
                    event.update(detail)
        return self.convert(ctx, candidate.key, candidate.url, event)

    async def revisit(self, ctx: SourceContext, tracked: TrackedEvent) -> list[EventImport] | None:
        slug = tracked.source_id.split(":", 1)[1]
        detail = await self.call(ctx, "get_hackathon", {"slug": slug})
        if not isinstance(detail, dict):
            raise FetchError(self.options.endpoint, "hackalendar: expected an object")
        url = web_url(detail.get("url")) or self.options.endpoint
        return self.convert(ctx, slug, url, detail)

    def convert(
        self, ctx: SourceContext, slug: str, url: str, event: dict[str, Any]
    ) -> list[EventImport]:
        country = country_code(event.get("countryCode"))
        fmt = MODES.get(str(event.get("mode")))
        if not ctx.in_scope(country, fmt):
            return []
        precision = event.get("temporalPrecision")
        precision = precision if isinstance(precision, dict) else {}
        published = zone_name(event.get("timezone"))
        zone = published or zone_for(country)
        starts_at, start_inferred = boundary(
            event.get("startsAt"), precision.get("start"), zone, end=False
        )
        ends_at, end_inferred = boundary(event.get("endsAt"), precision.get("end"), zone, end=True)
        title = clean_text(event.get("name"))
        if title is None or starts_at is None or ends_at is None or ends_at < ctx.cutoff:
            return []
        if ends_at < starts_at:
            ends_at = day_end(starts_at.date(), zone)
        deadline, _ = boundary(event.get("registrationDeadline"), None, zone, end=True)
        description = clean_text(event.get("description"))
        low, high = team_size(description)
        themes = [str(theme) for theme in event.get("themes") or [] if theme]
        pool = clean_text(event.get("prizePool"))
        amount, currency = money(pool)
        place = clean_text(event.get("location"))
        city = city_name(place) if place and place.casefold() not in NOT_A_CITY else None
        inferred = [
            name
            for name, flag in (
                ("timezone", zone is not None and precision.get("timezone") != "published"),
                ("starts_at", start_inferred),
                ("ends_at", end_inferred),
            )
            if flag
        ]
        fields: dict[str, Any] = {
            "status": "cancelled" if event.get("cancelled") is True else "announced",
            "kind": EventKind.HACKATHON,
            "title": title,
            "summary": clean_text(event.get("pickNote")) or summary_of(description),
            "description_md": description,
            "organizer": clean_text(event.get("organizer")),
            "website_url": web_url(event.get("url")) or url,
            "registration_url": web_url(event.get("registrationUrl")),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "registration_deadline": deadline if deadline and deadline < ends_at else None,
            "team_size_min": low,
            "team_size_max": high,
            "timezone": zone,
            "format": fmt,
            "location": {
                key: value
                for key, value in (
                    ("country", country),
                    ("city", city if fmt != EventFormat.ONLINE else None),
                    ("address", clean_text(event.get("venueName"))),
                )
                if value
            }
            or None,
            "themes": themes[:10],
            "areas": map_areas([*slug_terms(themes), *text_terms(title)], ctx.areas),
            "prize": {"amount": amount, "currency": currency, "description": pool}
            if pool
            else None,
            "confidence": ctx.confidence(0.85),
            "inferred_fields": inferred,
        }
        urls = [url for url in (fields["website_url"], fields["registration_url"]) if url]
        return [self.build(ctx, slug, urls, fields)]
