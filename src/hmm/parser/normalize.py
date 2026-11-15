import html
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import HttpUrl, TypeAdapter, ValidationError

from hmm.catalog.import_schema import ImportArea
from hmm.common import AreaImportance, EventKind

COUNTRY_ZONES: dict[str, str] = {
    "AD": "Europe/Andorra",
    "AL": "Europe/Tirane",
    "AT": "Europe/Vienna",
    "BA": "Europe/Sarajevo",
    "BE": "Europe/Brussels",
    "BG": "Europe/Sofia",
    "CH": "Europe/Zurich",
    "CY": "Asia/Nicosia",
    "CZ": "Europe/Prague",
    "DE": "Europe/Berlin",
    "DK": "Europe/Copenhagen",
    "EE": "Europe/Tallinn",
    "ES": "Europe/Madrid",
    "FI": "Europe/Helsinki",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "GR": "Europe/Athens",
    "HR": "Europe/Zagreb",
    "HU": "Europe/Budapest",
    "IE": "Europe/Dublin",
    "IS": "Atlantic/Reykjavik",
    "IT": "Europe/Rome",
    "LI": "Europe/Vaduz",
    "LT": "Europe/Vilnius",
    "LU": "Europe/Luxembourg",
    "LV": "Europe/Riga",
    "MC": "Europe/Monaco",
    "MD": "Europe/Chisinau",
    "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje",
    "MT": "Europe/Malta",
    "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "RS": "Europe/Belgrade",
    "SE": "Europe/Stockholm",
    "SI": "Europe/Ljubljana",
    "SK": "Europe/Bratislava",
    "SM": "Europe/San_Marino",
    "UA": "Europe/Kyiv",
    "VA": "Europe/Vatican",
    "XK": "Europe/Belgrade",
}

EUROPE: frozenset[str] = frozenset(COUNTRY_ZONES)

COUNTRY_NAMES: dict[str, str] = {
    "albania": "AL",
    "andorra": "AD",
    "austria": "AT",
    "belgium": "BE",
    "bosnia and herzegovina": "BA",
    "bosnia": "BA",
    "bulgaria": "BG",
    "croatia": "HR",
    "cyprus": "CY",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "england": "GB",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "great britain": "GB",
    "greece": "GR",
    "hungary": "HU",
    "iceland": "IS",
    "ireland": "IE",
    "italy": "IT",
    "kosovo": "XK",
    "latvia": "LV",
    "liechtenstein": "LI",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "moldova": "MD",
    "monaco": "MC",
    "montenegro": "ME",
    "netherlands": "NL",
    "the netherlands": "NL",
    "north macedonia": "MK",
    "macedonia": "MK",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "san marino": "SM",
    "scotland": "GB",
    "serbia": "RS",
    "srbija": "RS",
    "србија": "RS",
    "slovakia": "SK",
    "slovenia": "SI",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "ukraine": "UA",
    "united kingdom": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.a.": "US",
    "vatican city": "VA",
    "wales": "GB",
}

CITY_NAMES: dict[str, str] = {
    "beograd": "Belgrade",
    "београд": "Belgrade",
    "belgrade": "Belgrade",
    "novi sad": "Novi Sad",
    "нови сад": "Novi Sad",
    "nis": "Niš",
    "niš": "Niš",
    "ниш": "Niš",
    "kragujevac": "Kragujevac",
    "крагујевац": "Kragujevac",
    "subotica": "Subotica",
    "суботица": "Subotica",
}

AREA_TERMS: dict[str, str] = {
    "ai": "ml_ai",
    "artificial intelligence": "ml_ai",
    "machine learning": "ml_ai",
    "ml": "ml_ai",
    "llm": "ml_ai",
    "genai": "ml_ai",
    "generative ai": "ml_ai",
    "deep learning": "ml_ai",
    "computer vision": "ml_ai",
    "nlp": "ml_ai",
    "data": "data",
    "data science": "data",
    "big data": "data",
    "analytics": "data",
    "database": "data",
    "security": "security",
    "cybersecurity": "security",
    "cyber security": "security",
    "ctf": "security",
    "game": "gamedev",
    "games": "gamedev",
    "gamedev": "gamedev",
    "gaming": "gamedev",
    "game jam": "gamedev",
    "gamejam": "gamedev",
    "hardware": "hardware",
    "iot": "hardware",
    "robotics": "hardware",
    "embedded": "hardware",
    "electronics": "hardware",
    "devops": "devops",
    "cloud": "devops",
    "kubernetes": "devops",
    "infrastructure": "devops",
    "mobile": "mobile",
    "android": "mobile",
    "ios": "mobile",
    "design": "design",
    "ux": "design",
    "ui": "design",
    "frontend": "frontend",
    "web": "frontend",
    "backend": "backend",
    "api": "backend",
    "product": "product",
    "startup": "product",
    "entrepreneurship": "product",
    "business": "product",
    "testing": "qa",
    "qa": "qa",
}

KIND_TERMS: tuple[tuple[str, EventKind], ...] = (
    ("hackathon", EventKind.HACKATHON),
    ("hakaton", EventKind.HACKATHON),
    ("хакатон", EventKind.HACKATHON),
    ("game jam", EventKind.CONTEST),
    ("gamejam", EventKind.CONTEST),
    ("ctf", EventKind.CONTEST),
    ("competition", EventKind.CONTEST),
    ("challenge", EventKind.CONTEST),
    ("contest", EventKind.CONTEST),
    ("workshop", EventKind.WORKSHOP),
    ("radionica", EventKind.WORKSHOP),
    ("bootcamp", EventKind.COURSE),
    ("course", EventKind.COURSE),
    ("conference", EventKind.CONFERENCE),
    ("konferencija", EventKind.CONFERENCE),
    ("summit", EventKind.CONFERENCE),
    ("devfest", EventKind.CONFERENCE),
    ("meetup", EventKind.MEETUP),
)

MONTHS: dict[str, int] = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}

CURRENCY_SYMBOLS: dict[str, str] = {"€": "EUR", "£": "GBP", "$": "USD", "₹": "INR"}
CURRENCIES: frozenset[str] = frozenset(
    {
        "AUD",
        "BGN",
        "CAD",
        "CHF",
        "CZK",
        "DKK",
        "EUR",
        "GBP",
        "HUF",
        "INR",
        "JPY",
        "NOK",
        "PLN",
        "RON",
        "RSD",
        "SEK",
        "USD",
    }
)

_MONEY = re.compile(
    r"(?:(?P<sym>[€£$₹])\s*(?P<code>[A-Za-z]{3}\b)?|(?P<pre>\b[A-Za-z]{3}\b))?\s*"
    r"(?P<num>\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.\d+)?\s*(?P<post>[A-Za-z]{3}\b|[€£$₹])?"
)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)(\S(?:.*?\S)?)\1")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

_TEAM_RANGE = re.compile(
    r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:players|members|participants|people|persons)\b"
    r"|\bteams?\s+of\s+(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\b",
    re.IGNORECASE,
)
_TEAM_MAX = re.compile(
    r"\bteams?\s+(?:of\s+)?(?:size\s*:?\s*)?up\s+to\s+(\d{1,2})\b"
    r"|\bteam\s+size\s*:?\s*(?:up\s+to|max(?:imum)?\.?)\s*(\d{1,2})\b"
    r"|\bmax(?:imum)?\.?\s+(\d{1,2})\s+(?:players|members|participants)\s+per\s+team\b",
    re.IGNORECASE,
)


_URL = TypeAdapter(HttpUrl)


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return None
    if not re.match(r"^https?://", text, re.IGNORECASE):
        if not re.match(r"^(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:[/?#]|$)", text):
            return None
        text = f"https://{text}"
    try:
        _URL.validate_python(text)
    except ValidationError:
        return None
    return text


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return text.upper()
    return COUNTRY_NAMES.get(text.casefold())


def country_in_text(value: str) -> str | None:
    lowered = f" {value.casefold()} "
    for name, code in sorted(COUNTRY_NAMES.items(), key=lambda pair: -len(pair[0])):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered):
            return code
    return None


def city_name(value: str | None) -> str | None:
    if not value:
        return None
    text = unicodedata.normalize("NFC", value.strip())
    return CITY_NAMES.get(text.casefold(), text) or None


def zone_for(country: str | None) -> str | None:
    return COUNTRY_ZONES.get(country) if country else None


def zone_name(value: object) -> str | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


def money(value: str | None) -> tuple[float | None, str | None]:
    if not value:
        return None, None
    for match in _MONEY.finditer(value):
        currency = None
        for part in (match["code"], match["pre"], match["post"], match["sym"]):
            if part is None:
                continue
            code = CURRENCY_SYMBOLS.get(part) or part.upper()
            if code in CURRENCIES:
                currency = code
                break
        amount = float(re.sub(r"[,\s]", "", match["num"]))
        if amount > 0 and currency is not None:
            return amount, currency
    return None, None


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return None
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def is_midnight(value: datetime) -> bool:
    return value.timetz().replace(tzinfo=None) == time(0, 0)


def day_start(day: date, zone: str | None) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(zone) if zone else UTC)


def day_end(day: date, zone: str | None) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=ZoneInfo(zone) if zone else UTC)


def epoch(value: datetime) -> int:
    return int(value.timestamp())


class _TextExtractor(HTMLParser):
    BLOCKS = frozenset({"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"})
    SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    if "<" not in value:
        return clean_text(html.unescape(value))
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return clean_text("".join(parser.parts))


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _INVISIBLE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return unicodedata.normalize("NFC", text) or None


def clip(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    if len(value) <= limit:
        return value
    cut = value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{cut}…"


def plain(markdown: str) -> str:
    text = _MD_IMAGE.sub("", markdown)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


def summary_of(value: str | None) -> str | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split("\n\n") if part.strip()]
    prose = [part for part in parts if not part.startswith("#")] or parts
    return clip(plain(prose[0]), 500) if prose else None


def team_size(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    match = _TEAM_RANGE.search(text)
    if match is not None:
        low, high = (int(value) for value in match.groups() if value is not None)
        if 1 <= low <= high <= 50:
            return low, high
    match = _TEAM_MAX.search(text)
    if match is not None:
        high = next(int(value) for value in match.groups() if value is not None)
        if 1 <= high <= 50:
            return None, high
    return None, None


def slug_terms(values: Iterable[str]) -> list[str]:
    terms: list[str] = []
    for value in values:
        term = " ".join(re.split(r"[-_/\s]+", value.casefold())).strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def text_terms(*values: str | None) -> list[str]:
    terms: list[str] = []
    for value in values:
        if not value:
            continue
        words = re.findall(r"[a-z0-9]+", value.casefold())
        terms.extend(words)
        terms.extend(f"{left} {right}" for left, right in pairwise(words))
    return terms


def map_areas(
    terms: Iterable[str], known: frozenset[str], *, primary: str | None = None
) -> list[ImportArea]:
    keys: list[str] = []
    if primary is not None and primary in known:
        keys.append(primary)
    for term in slug_terms(terms):
        key = AREA_TERMS.get(term)
        if key is not None and key in known and key not in keys:
            keys.append(key)
    return [
        ImportArea(
            key=key, importance=AreaImportance.PRIMARY if index == 0 else AreaImportance.NICE
        )
        for index, key in enumerate(keys)
    ]


def guess_kind(title: str, terms: Iterable[str], default: EventKind) -> EventKind:
    tail = re.split(r"\s[-\u2013\u2014|]\s|:\s", title)[-1].strip().casefold()
    for term, kind in KIND_TERMS:
        if tail == term or tail == f"{term}s":
            return kind
    text = " ".join([title, *terms]).casefold()
    for term, kind in KIND_TERMS:
        if re.search(rf"(?<![a-zа-я]){re.escape(term)}", text):
            return kind
    return default
