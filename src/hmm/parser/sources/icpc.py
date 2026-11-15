import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.normalize import (
    as_dict,
    city_name,
    clean_text,
    country_code,
    country_in_text,
    day_end,
    day_start,
    parse_date,
    web_url,
    zone_for,
)

DELIVERY = {
    "ONSITE": EventFormat.OFFLINE,
    "ONLINE": EventFormat.ONLINE,
    "HYBRID": EventFormat.HYBRID,
}
_PLACEHOLDER_SITE = re.compile(r"delete me|unofficial", re.IGNORECASE)


class IcpcOptions(Contract):
    api: Annotated[str, Field(pattern=r"^https://")] = "https://icpc.global/api/contest/public"
    finder: Annotated[str, Field(pattern=r"^https://")] = "https://icpc.global/regionals/finder"
    regions: Annotated[tuple[str, ...], Field(min_length=1)] = (
        "seerc",
        "central-europe",
        "northwestern-europe",
        "swerc",
    )


def real_sites(contest: dict[str, Any]) -> list[dict[str, Any]]:
    sites = [
        site
        for site in contest.get("sites") or []
        if isinstance(site, dict)
        and site.get("siteType") == "NORMAL"
        and isinstance(site.get("id"), int)
        and not _PLACEHOLDER_SITE.search(str(site.get("name") or ""))
    ]
    dated = [site for site in sites if site.get("siteDate")]
    return dated or sites


def site_country(site: dict[str, Any]) -> str | None:
    address = as_dict(site.get("address"))
    country = as_dict(address.get("country"))
    return country_code(country.get("a2")) or country_in_text(
        " ".join(str(site.get(key) or "") for key in ("name", "locationName"))
    )


@adapter
class Icpc(Adapter[IcpcOptions]):
    name = "icpc"
    options_model = IcpcOptions

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        for slug in self.options.regions:
            response = await ctx.get(
                f"{self.options.api.rstrip('/')}/{slug}", accept=frozenset({200, 404})
            )
            if response.status == 404:
                ctx.log.info("parser %s: no current contest for %s", ctx.source.id, slug)
                continue
            contest = response.json()
            if isinstance(contest, dict) and isinstance(contest.get("id"), int):
                yield Candidate(
                    key=str(contest["id"]),
                    url=f"{self.options.finder.rstrip('/')}/{slug}",
                    payload=contest,
                )

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        contest: dict[str, Any] = candidate.payload
        title = clean_text(contest.get("name"))
        start_day = parse_date(contest.get("startDate"))
        end_day = parse_date(contest.get("endDate")) or start_day
        if title is None or start_day is None or end_day is None or end_day < start_day:
            return []
        sites = real_sites(contest)
        opens_day = parse_date(contest.get("regStartDate"))
        closes_day = parse_date(contest.get("regEndDate"))
        area = clean_text(contest.get("geographicArea"))
        website = web_url(contest.get("homepage")) or candidate.url
        items: list[EventImport] = []
        for site in sites:
            country = site_country(site)
            fmt = DELIVERY.get(str(site.get("siteDelivery"))) or DELIVERY.get(
                str(contest.get("contestDelivery"))
            )
            if not ctx.in_scope(country, fmt):
                continue
            zone = zone_for(country)
            starts_at, ends_at = day_start(start_day, zone), day_end(end_day, zone)
            if ends_at < ctx.cutoff:
                continue
            address = as_dict(site.get("address"))
            city = city_name(address.get("city"))
            label = city or clean_text(site.get("name"))
            opens = day_start(opens_day, zone) if opens_day else None
            deadline = day_end(closes_day, zone) if closes_day else None
            fields: dict[str, Any] = {
                "kind": EventKind.CONTEST,
                "title": f"{title} \u2014 {label}" if len(sites) > 1 and label else title,
                "organizer": "ICPC",
                "website_url": website,
                "registration_url": candidate.url,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "registration_opens_at": opens if opens and opens < starts_at else None,
                "registration_deadline": deadline if deadline and deadline < starts_at else None,
                "timezone": zone,
                "format": fmt,
                "location": {
                    key: value
                    for key, value in (
                        ("country", country),
                        ("city", city),
                        ("address", clean_text(site.get("locationName"))),
                    )
                    if value
                }
                or None,
                "eligibility": f"University teams from {area}" if area else None,
                "themes": ["ICPC", "competitive programming"],
                "team_size_min": 3,
                "team_size_max": 3,
                "confidence": ctx.confidence(0.85),
                "inferred_fields": [
                    *(["timezone"] if zone else []),
                    "starts_at",
                    "ends_at",
                    "team_size_min",
                    "team_size_max",
                ],
            }
            items.append(
                self.build(ctx, f"{candidate.key}:{site['id']}", [candidate.url, website], fields)
            )
        return items
