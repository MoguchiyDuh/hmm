from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hmm.bot import texts_en, texts_ru
from hmm.bot.i18n import Noun, Texts
from hmm.common import EventFormat, Locale
from hmm.notifications.payloads import NotificationChange, NotificationPerson, ScoreChange

TEXTS: dict[Locale, Texts] = {Locale.EN: texts_en.TEXTS, Locale.RU: texts_ru.TEXTS}

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@dataclass(frozen=True, slots=True)
class View:
    public_url: str
    now: datetime | None = None
    timezone: str | None = None
    link: str | None = None
    locale: Locale = Locale.EN


VIEW: ContextVar[View | None] = ContextVar("hmm_bot_view", default=None)


@contextmanager
def viewing(view: View) -> Generator[View]:
    token = VIEW.set(view)
    try:
        yield view
    finally:
        VIEW.reset(token)


@contextmanager
def localized(locale: Locale) -> Generator[View]:
    current = VIEW.get() or View(public_url="")
    with viewing(replace(current, locale=locale)) as view:
        yield view


def current_locale() -> Locale:
    view = VIEW.get()
    return view.locale if view else Locale.EN


def tr() -> Texts:
    return TEXTS[current_locale()]


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
    texts = tr()
    if seconds < HOUR:
        return plural(round(seconds / MINUTE), texts.minute)
    if seconds < 2 * DAY:
        return plural(round(seconds / HOUR), texts.hour)
    return plural(round(seconds / DAY), texts.day)


def relative(moment: datetime) -> str | None:
    view = VIEW.get()
    if view is None or view.now is None:
        return None
    seconds = (moment - view.now).total_seconds()
    texts = tr()
    if abs(seconds) < MINUTE:
        return texts.just_now
    return (texts.in_time if seconds >= 0 else texts.ago_time).format(ago(seconds))


def date_label(local: datetime) -> str:
    texts = tr()
    return f"{texts.weekdays[local.weekday()]} {local:%d} {texts.months[local.month - 1]}"


def when(moment: datetime, timezone: str | None = None, *, relative_to_now: bool = True) -> str:
    local = moment.astimezone(zone(timezone))
    text = f"{date_label(local)} {local:%H:%M} {local.tzname()}"
    hint = relative(moment) if relative_to_now else None
    return f"{text} ({hint})" if hint else text


def day(moment: datetime, timezone: str | None = None) -> str:
    return date_label(moment.astimezone(zone(timezone)))


def span(start: datetime, end: datetime, timezone: str | None = None) -> str:
    first = start.astimezone(zone(timezone))
    last = end.astimezone(zone(timezone))
    hint = relative(start)
    suffix = f" ({hint})" if hint else ""
    if first.date() == last.date():
        return f"{date_label(first)} {first:%H:%M}-{last:%H:%M} {last.tzname()}{suffix}"
    return tr().span_days.format(date_label(first), date_label(last)) + suffix


def score(value: float) -> str:
    return f"{value:.1f}"


def humanize(value: object) -> str:
    return str(value).replace("_", " ")


def person(value: NotificationPerson) -> str:
    return bold(value.display_name)


def people(values: tuple[NotificationPerson, ...], limit: int = 10) -> str:
    names = [esc(value.display_name) for value in values[:limit]]
    if len(values) > limit:
        names.append(tr().and_more.format(len(values) - limit))
    return ", ".join(names)


def reason(value: str) -> str:
    return esc(value.strip().rstrip("."))


def plural(count: int, noun: Noun) -> str:
    return f"{count} {noun.pick(count)}"


def headcount(count: int) -> str:
    return plural(count, tr().person)


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


def event_field(field: str) -> str:
    return tr().event_fields.get(field, humanize(field))


def change_line(change: NotificationChange, timezone: str) -> str:
    return (
        f"• {esc(event_field(change.field))}: "
        f"{change_value(change.field, change.previous, timezone)} → "
        f"{change_value(change.field, change.current, timezone)}"
    )


def change_value(field: str, value: str | None, timezone: str) -> str:
    if field == "format" and value in EventFormat:
        return esc(tr().event_formats[EventFormat(value)])
    return moment_or_text(value, timezone)


def score_changes(changes: tuple[ScoreChange, ...]) -> str:
    return "\n".join(
        f"• {esc(change.area_name)}: {score(change.previous_score)} → "
        f"{bold(score(change.score))} — {esc(clip(change.reason, 160))}"
        for change in changes
    )
