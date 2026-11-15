from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from hmm.notifications.payloads import NotificationPerson


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


def clip(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


def quote(value: str | None, limit: int = 400) -> str | None:
    return (
        f"<blockquote>{esc(clip(value, limit))}</blockquote>" if value and value.strip() else None
    )


def when(moment: datetime, timezone: str | None = None) -> str:
    local = moment.astimezone(ZoneInfo(timezone) if timezone else UTC)
    return f"{local:%a %d %b %H:%M} {local.tzname()}"


def day(moment: datetime, timezone: str | None = None) -> str:
    local = moment.astimezone(ZoneInfo(timezone) if timezone else UTC)
    return f"{local:%a %d %b}"


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


def moment_or_text(value: str | None, timezone: str | None = None) -> str:
    if value is None:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return esc(value)
    return when(parsed, timezone) if parsed.tzinfo else esc(value)


def lines(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)
