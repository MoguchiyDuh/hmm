from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hmm.notifications.payloads import NotificationPerson

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@dataclass(frozen=True, slots=True)
class View:
    public_url: str
    now: datetime | None = None
    timezone: str | None = None
    link: str | None = None


VIEW: ContextVar[View | None] = ContextVar("hmm_bot_view", default=None)


@contextmanager
def viewing(view: View) -> Generator[View]:
    token = VIEW.set(view)
    try:
        yield view
    finally:
        VIEW.reset(token)


def web_url(path: str | None = None) -> str | None:
    view = VIEW.get()
    if view is None or not view.public_url.startswith("https://"):
        return None
    target = path if path is not None else view.link
    return f"{view.public_url}{target}" if target is not None else None


def esc(value: object) -> str:
    return escape(str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def italic(value: object) -> str:
    return f"<i>{esc(value)}</i>"


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{esc(label)}</a>'


def subject(value: object, path: str | None = None) -> str:
    url = web_url(path)
    if url is None:
        return bold(value)
    return f'<a href="{escape(url, quote=True)}">{bold(value)}</a>'


def clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


def quote(value: str | None, limit: int = 400) -> str | None:
    if value is None or not value.strip():
        return None
    return f"<blockquote>{esc(clip(value, limit))}</blockquote>"


def zone(timezone: str | None) -> tzinfo:
    view = VIEW.get()
    for name in (view.timezone if view else None, timezone):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return UTC


def ago(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < MINUTE:
        return "under a minute"
    if seconds < HOUR:
        return plural(round(seconds / MINUTE), "minute")
    if seconds < 2 * DAY:
        return plural(round(seconds / HOUR), "hour")
    return plural(round(seconds / DAY), "day")


def relative(moment: datetime) -> str | None:
    view = VIEW.get()
    if view is None or view.now is None:
        return None
    seconds = (moment - view.now).total_seconds()
    return f"in {ago(seconds)}" if seconds >= 0 else f"{ago(seconds)} ago"


def when(moment: datetime, timezone: str | None = None, *, relative_to_now: bool = True) -> str:
    local = moment.astimezone(zone(timezone))
    text = f"{local:%a %d %b %H:%M} {local.tzname()}"
    hint = relative(moment) if relative_to_now else None
    return f"{text} ({hint})" if hint else text


def day(moment: datetime, timezone: str | None = None) -> str:
    return f"{moment.astimezone(zone(timezone)):%a %d %b}"


def span(start: datetime, end: datetime, timezone: str | None = None) -> str:
    first = start.astimezone(zone(timezone))
    last = end.astimezone(zone(timezone))
    hint = relative(start)
    suffix = f" ({hint})" if hint else ""
    if first.date() == last.date():
        return f"{first:%a %d %b %H:%M}-{last:%H:%M} {last.tzname()}{suffix}"
    return f"{first:%a %d %b} to {last:%a %d %b}{suffix}"


def score(value: float) -> str:
    return f"{value:.1f}"


def humanize(value: object) -> str:
    return str(value).replace("_", " ")


def person(value: NotificationPerson) -> str:
    return bold(value.display_name)


def people(values: tuple[NotificationPerson, ...], limit: int = 10) -> str:
    names = [esc(value.display_name) for value in values[:limit]]
    if len(values) > limit:
        names.append(f"+{len(values) - limit} more")
    return ", ".join(names)


def reason(value: str) -> str:
    return esc(value.strip().rstrip("."))


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def headcount(count: int) -> str:
    return "1 person" if count == 1 else f"{count} people"


def moment_or_text(value: str | None, timezone: str | None = None) -> str:
    if value is None:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return esc(value)
    return when(parsed, timezone, relative_to_now=False) if parsed.tzinfo else esc(value)


def lines(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)
