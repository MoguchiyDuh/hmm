import re
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
    clip,
    country_code,
    guess_kind,
    map_areas,
    parse_datetime,
    slug_terms,
    summary_of,
    text_terms,
    web_url,
)

type Doc = dict[str, Any]


class LumaOptions(Contract):
    api: Annotated[str, Field(pattern=r"^https://")] = "https://api.lu.ma"
    site: Annotated[str, Field(pattern=r"^https://")] = "https://luma.com"
    calendars: Annotated[
        list[Annotated[str, Field(pattern=r"^cal-[A-Za-z0-9]+$")]], Field(min_length=1)
    ]
    page_limit: Annotated[int, Field(ge=1, le=50)] = 50
    max_pages: Annotated[int, Field(ge=1)] = 10
    details: bool = True
    kind: EventKind = EventKind.MEETUP


def inline(nodes: list[Doc]) -> str:
    parts: list[str] = []
    for node in nodes:
        kind = node.get("type")
        if kind in {"hard_break", "hardBreak"}:
            parts.append("  \n")
            continue
        if kind != "text":
            parts.append(inline(node.get("content") or []))
            continue
        text = str(node.get("text") or "")
        if not text.strip():
            parts.append(text)
            continue
        core = text.strip()
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        for mark in node.get("marks") or []:
            name = mark.get("type")
            if name == "bold":
                core = f"**{core}**"
            elif name == "italic":
                core = f"*{core}*"
            elif name == "code":
                core = f"`{core}`"
            elif name == "strike":
                core = f"~~{core}~~"
            elif name == "link":
                href = web_url((mark.get("attrs") or {}).get("href"))
                if href:
                    core = f"[{core}]({href})"
        parts.append(f"{lead}{core}{trail}")
    return "".join(parts)


def block(node: Doc, depth: int = 0) -> list[str]:
    kind = node.get("type")
    content: list[Doc] = node.get("content") or []
    attrs = node.get("attrs") or {}
    if kind == "paragraph":
        text = inline(content).strip()
        return [text] if text else []
    if kind == "heading":
        level = min(max(int(attrs.get("level") or 2), 1), 6)
        text = inline(content).strip()
        return [f"{'#' * level} {text}"] if text else []
    if kind in {"bullet_list", "bulletList", "ordered_list", "orderedList"}:
        ordered = kind.startswith("ordered")
        start = int(attrs.get("order") or attrs.get("start") or 1)
        lines: list[str] = []
        for number, item in enumerate(content, start=start):
            marker = f"{number}." if ordered else "-"
            inner = [
                line for child in item.get("content") or [] for line in block(child, depth + 1)
            ]
            if not inner:
                continue
            pad = "  " * depth
            lines.append(f"{pad}{marker} {inner[0]}")
            lines.extend(f"{pad}  {line}" for line in inner[1:])
        return ["\n".join(lines)] if lines else []
    if kind == "blockquote":
        inner = [line for child in content for line in block(child, depth)]
        return (
            ["\n".join(f"> {line}" for text in inner for line in text.split("\n"))] if inner else []
        )
    if kind in {"code_block", "codeBlock"}:
        return [f"```\n{inline(content)}\n```"]
    if kind in {"horizontal_rule", "horizontalRule"}:
        return ["---"]
    return [line for child in content for line in block(child, depth)]


def to_markdown(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return None
    text = "\n\n".join(block(doc)).strip()
    return text or None


@adapter
class Luma(Adapter[LumaOptions]):
    name = "luma"
    options_model = LumaOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        seen: set[str] = set()
        endpoint = f"{self.options.api}/calendar/get-items"
        for calendar in self.options.calendars:
            for period in ("future", "past"):
                cursor: str | None = None
                for _ in range(self.options.max_pages):
                    params: dict[str, str | int] = {
                        "calendar_api_id": calendar,
                        "period": period,
                        "pagination_limit": self.options.page_limit,
                    }
                    if cursor:
                        params["pagination_cursor"] = cursor
                    response = await ctx.get(endpoint, params=params)
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("entries"), list):
                        raise FetchError(response.url, "luma: unexpected get-items payload")
                    stale = False
                    for entry in body["entries"]:
                        event = entry.get("event") if isinstance(entry, dict) else None
                        if not isinstance(event, dict) or not event.get("api_id"):
                            continue
                        ends_at = parse_datetime(event.get("end_at") or event.get("start_at"))
                        if ends_at is not None and ends_at < ctx.cutoff:
                            stale = True
                            continue
                        if event["api_id"] not in seen:
                            seen.add(event["api_id"])
                            yield Candidate(key=event["api_id"], url=endpoint, payload=entry)
                    cursor = body.get("next_cursor")
                    if not body.get("has_more") or not cursor or (period == "past" and stale):
                        break

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        entry: Doc = candidate.payload
        event: Doc = entry["event"]
        geo: Doc = event.get("geo_address_info") or {}
        country = country_code(geo.get("country_code")) or country_code(geo.get("country"))
        fmt = (
            EventFormat.OFFLINE
            if event.get("location_type") == "offline" or geo
            else EventFormat.ONLINE
        )
        if not ctx.in_scope(country, fmt):
            return []
        starts_at = parse_datetime(event.get("start_at"))
        ends_at = parse_datetime(event.get("end_at")) or starts_at
        title = clean_text(event.get("name"))
        if starts_at is None or ends_at is None or title is None:
            return []
        description: str | None = None
        language: list[str] = []
        urls = [candidate.url]
        if self.options.details:
            detail_url = f"{self.options.api}/event/get"
            detail = (await ctx.get(detail_url, params={"event_api_id": candidate.key})).json()
            if isinstance(detail, dict):
                description = clip(to_markdown(detail.get("description_mirror")), 20000)
                locale = str(detail.get("locale") or "")[:2].lower()
                language = [locale] if re.fullmatch(r"[a-z]{2}", locale) else []
        calendar = entry.get("calendar") or {}
        hosts = [str(host["name"]) for host in entry.get("hosts") or [] if host.get("name")]
        organizer = calendar.get("name")
        if not organizer or organizer == "Personal":
            organizer = ", ".join(hosts[:3]) or None
        tags = [str(tag.get("name")) for tag in entry.get("tags") or [] if tag.get("name")]
        slug = event.get("url")
        website = f"{self.options.site}/{slug}" if slug else None
        fields: dict[str, Any] = {
            "kind": guess_kind(title, tags, self.options.kind),
            "title": title,
            "summary": summary_of(description),
            "description_md": description,
            "organizer": clean_text(organizer),
            "website_url": website,
            "registration_url": website,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "timezone": event.get("timezone"),
            "format": fmt,
            "location": {
                key: value
                for key, value in (
                    ("country", country),
                    ("city", city_name(geo.get("city"))),
                    ("address", clean_text(geo.get("full_address") or geo.get("address"))),
                )
                if value
            }
            or None,
            "language": language,
            "themes": tags[:10],
            "areas": map_areas([*slug_terms(tags), *text_terms(title)], ctx.areas),
            "confidence": ctx.confidence(0.85),
        }
        return [self.build(ctx, candidate.key, [*urls, *([website] if website else [])], fields)]
