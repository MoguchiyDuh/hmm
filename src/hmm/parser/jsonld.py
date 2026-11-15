import json
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from hmm.common import EventFormat
from hmm.parser.normalize import (
    city_name,
    clean_text,
    country_code,
    country_in_text,
    day_end,
    day_start,
    html_to_text,
    is_midnight,
    parse_date,
    parse_datetime,
    summary_of,
    web_url,
    zone_for,
)

EVENT_TYPES = frozenset(
    {
        "Event",
        "EducationEvent",
        "BusinessEvent",
        "SocialEvent",
        "Hackathon",
        "ExhibitionEvent",
        "Festival",
        "CourseInstance",
    }
)
ATTENDANCE = {
    "OfflineEventAttendanceMode": EventFormat.OFFLINE,
    "OnlineEventAttendanceMode": EventFormat.ONLINE,
    "MixedEventAttendanceMode": EventFormat.HYBRID,
}

_SCRIPT = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

type Node = dict[str, Any]


def blocks(html: str) -> list[Any]:
    found: list[Any] = []
    for match in _SCRIPT.finditer(html):
        body = match[1].strip()
        if body.startswith("<!--"):
            body = body.removeprefix("<!--").removesuffix("-->").strip()
        try:
            found.append(json.loads(body, strict=False))
        except ValueError:
            continue
    return found


def nodes(data: Any) -> Iterator[Node]:
    if isinstance(data, list):
        for item in data:
            yield from nodes(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from nodes(graph)
        if "@type" in data:
            yield data


def types_of(node: Node) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(item).rsplit("/", 1)[-1] for item in values if item}


def events(html: str) -> tuple[list[Node], dict[str, Node]]:
    found: list[Node] = []
    index: dict[str, Node] = {}
    for block in blocks(html):
        for node in nodes(block):
            if isinstance(node.get("@id"), str):
                index.setdefault(node["@id"], node)
            if types_of(node) & EVENT_TYPES:
                found.append(node)
    return found, index


def resolve(value: Any, index: dict[str, Node]) -> Any:
    if isinstance(value, dict) and set(value) == {"@id"}:
        return index.get(value["@id"], value)
    return value


def first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def text(value: Any) -> str | None:
    value = first(value)
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value")
    return clean_text(str(value)) if isinstance(value, (str, int, float)) and value != "" else None


def url(value: Any) -> str | None:
    value = first(value)
    if isinstance(value, dict):
        value = value.get("url") or value.get("@id")
    if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
        return web_url(value)
    return None


def event_format(node: Node) -> EventFormat | None:
    mode = text(node.get("eventAttendanceMode"))
    if mode is None:
        return None
    return ATTENDANCE.get(mode.rsplit("/", 1)[-1])


def location(node: Node, index: dict[str, Node]) -> dict[str, str | None]:
    place = resolve(first(node.get("location")), index)
    if not isinstance(place, dict) or "VirtualLocation" in types_of(place):
        return {}
    address = resolve(place.get("address"), index)
    name = text(place.get("name"))
    if isinstance(address, str):
        return {"country": country_in_text(address), "city": None, "address": address}
    if not isinstance(address, dict):
        return {"country": country_in_text(name) if name else None, "city": None, "address": None}
    region = text(address.get("addressRegion"))
    country = (
        country_code(text(address.get("addressCountry")))
        or country_code(region)
        or country_in_text(" ".join(part for part in (region, name) if part))
    )
    street = text(address.get("streetAddress"))
    city = city_name(text(address.get("addressLocality")))
    venue = name if name and name != city and "," not in name else None
    return {
        "country": country,
        "city": city,
        "address": ", ".join(part for part in (venue, street) if part) or None,
    }


def organizer(node: Node, index: dict[str, Node]) -> str | None:
    values = node.get("organizer")
    for value in values if isinstance(values, list) else [values]:
        resolved = resolve(value, index)
        if isinstance(resolved, dict) and "Person" not in types_of(resolved):
            name = text(resolved.get("name"))
            if name:
                return name
        elif isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
    return None


def dates(start: Any, end: Any, zone: str | None) -> tuple[datetime, datetime, bool] | None:
    start_at, end_at = parse_datetime(start), parse_datetime(end)
    start_day, end_day = parse_date(start), parse_date(end)
    if start_day is None:
        return None
    end_day = end_day or start_day
    if start_at is None or (is_midnight(start_at) and (end_at is None or is_midnight(end_at))):
        if end_day < start_day:
            return None
        return day_start(start_day, zone), day_end(end_day, zone), True
    if end_at is None:
        end_at = day_end(end_day, zone) if end_day > start_day else start_at
    return start_at, end_at, False


def fields(node: Node, index: dict[str, Node], page_url: str | None) -> dict[str, Any] | None:
    title = text(node.get("name"))
    if title is None:
        return None
    place = location(node, index)
    fmt = event_format(node) or (EventFormat.OFFLINE if place.get("country") else None)
    zone = zone_for(place.get("country"))
    window = dates(node.get("startDate"), node.get("endDate"), zone)
    if window is None:
        return None
    starts_at, ends_at, date_only = window
    description = html_to_text(text(node.get("description")))
    status = text(node.get("eventStatus")) or ""
    offer = resolve(first(node.get("offers")), index)
    website = url(node.get("url")) or page_url
    registration = url(offer) if isinstance(offer, dict) else None
    inferred = ["timezone"] if zone else []
    if date_only:
        inferred += ["starts_at", "ends_at"]
    deadline = parse_datetime(offer.get("validThrough")) if isinstance(offer, dict) else None
    return {
        "status": "cancelled" if status.endswith("EventCancelled") else "announced",
        "title": title,
        "summary": summary_of(description),
        "description_md": description,
        "organizer": organizer(node, index),
        "website_url": website,
        "registration_url": registration if registration != website else None,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "registration_deadline": deadline if deadline and deadline < starts_at else None,
        "timezone": zone,
        "format": fmt,
        "location": {key: value for key, value in place.items() if value} or None,
        "inferred_fields": inferred,
    }
