import re
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventKind
from hmm.parser import jsonld
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.normalize import guess_kind, map_areas, team_size, text_terms


class Listing(Contract):
    url: Annotated[str, Field(pattern=r"^https?://")]
    kind: EventKind | None = None


class JsonLdPagesOptions(Contract):
    listings: Annotated[
        list[Annotated[str, Field(pattern=r"^https?://")] | Listing], Field(min_length=1)
    ]
    follow: str | None = None
    id_pattern: str | None = None
    kind: EventKind = EventKind.MEETUP
    guess_kind: bool = True
    max_follow: Annotated[int, Field(ge=1)] = 100

    @field_validator("follow", "id_pattern")
    @classmethod
    def compile_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    def resolved(self) -> list[Listing]:
        return [item if isinstance(item, Listing) else Listing(url=item) for item in self.listings]


def slug_of(url: str) -> str | None:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    return parts[-1] if parts else None


@adapter
class JsonLdPages(Adapter[JsonLdPagesOptions]):
    name = "jsonld_pages"
    options_model = JsonLdPagesOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        seen: set[str] = set()
        follow = re.compile(self.options.follow) if self.options.follow else None
        for listing in self.options.resolved():
            kind = listing.kind or self.options.kind
            page = await ctx.get(listing.url)
            body = page.text()
            if follow is None:
                nodes, index = jsonld.events(body)
                for node in nodes:
                    key = jsonld.url(node.get("url")) or f"{page.url}#{len(seen)}"
                    if key not in seen:
                        seen.add(key)
                        yield Candidate(key=key, url=page.url, payload=(kind, node, index))
                continue
            for match in follow.finditer(body):
                target = match[0]
                if target in seen:
                    continue
                if len(seen) >= self.options.max_follow:
                    ctx.log.warning("parser %s: follow limit reached", ctx.source.id)
                    return
                seen.add(target)
                yield Candidate(key=target, url=target, payload=kind)

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        if isinstance(candidate.payload, tuple):
            kind, node, index = candidate.payload
            item = self.convert(ctx, kind, node, index, None)
            return [item] if item is not None else []
        page = await ctx.get(candidate.url)
        nodes, index = jsonld.events(page.text())
        items: list[EventImport] = []
        ids: set[str] = set()
        for node in nodes:
            item = self.convert(ctx, candidate.payload, node, index, page.url)
            if item is not None and item.source.source_id not in ids:
                ids.add(item.source.source_id)
                items.append(item)
        return items

    def local_id(self, url: str) -> str | None:
        if self.options.id_pattern is None:
            return slug_of(url)
        match = re.search(self.options.id_pattern, url.rstrip("/"))
        return match[1] if match is not None and match.groups() else None

    def convert(
        self,
        ctx: SourceContext,
        kind: EventKind,
        node: jsonld.Node,
        index: dict[str, jsonld.Node],
        page_url: str | None,
    ) -> EventImport | None:
        values = jsonld.fields(node, index, page_url)
        if values is None:
            return None
        website = values["website_url"]
        local = self.local_id(website) if website else None
        if local is None:
            return None
        place: dict[str, Any] = values["location"] or {}
        if not ctx.in_scope(place.get("country"), values["format"]):
            return None
        if values["ends_at"] < ctx.cutoff:
            return None
        title = values["title"]
        keywords = jsonld.text(node.get("keywords")) or ""
        short = values["summary"] if values["summary"] and len(values["summary"]) <= 160 else None
        low, high = team_size(values["description_md"])
        values.update(
            kind=guess_kind(title, [keywords], kind) if self.options.guess_kind else kind,
            areas=map_areas(text_terms(title, keywords, short), ctx.areas),
            themes=[term.strip() for term in keywords.split(",") if term.strip()][:10],
            team_size_min=low,
            team_size_max=high,
            confidence=ctx.confidence(0.8),
        )
        urls = [url for url in (page_url, website) if url]
        return self.build(ctx, local, urls, values)
