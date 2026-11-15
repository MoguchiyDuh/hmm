import re
from dataclasses import fields
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardButton

from hmm.bot import templates
from hmm.bot.callbacks import (
    ActionCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    HelpCallback,
    MenuItem,
)
from hmm.bot.commands import COMMANDS
from hmm.bot.delivery import Throttle
from hmm.bot.fmt import TEXTS
from hmm.bot.i18n import Texts, telegram_locale
from hmm.bot.templates import SILENT, TEMPLATES, render
from hmm.common import (
    EventFormat,
    EventKind,
    Locale,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    RestrictionKind,
    TeamStatus,
)
from hmm.notifications.payloads import (
    ActionKey,
    AlertKind,
    ApiAction,
    EventCancelledPayload,
    Notification,
    NotificationPayload,
    NotificationType,
    PartyInvitePayload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures/notifications"
PUBLIC_URL = "https://hmm.test"
ALLOWED_TAGS = {"b", "i", "code", "a", "blockquote"}
MAX_TEXT = 4096
MAX_CALLBACK_BYTES = 64
FACTORIES = (ActionCallback, HelpCallback, CheckinCallback, CloseCallback, ConfirmCallback)


class TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in ALLOWED_TAGS, tag
        assert tag == "a" or not attrs, (tag, attrs)
        if tag == "a":
            assert [name for name, _ in attrs] == ["href"]
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack.pop() == tag, tag


def assert_html(text: str) -> None:
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert checker.stack == []
    assert not re.search(r"&(?!(amp|lt|gt|quot);)", text), text


def fixture(path: Path) -> NotificationPayload:
    return Notification.model_validate_json(path.read_text()).payload


def buttons(rendered: templates.Rendered) -> list[InlineKeyboardButton]:
    return (
        [button for row in rendered.markup.inline_keyboard for button in row]
        if rendered.markup
        else []
    )


def unpack(data: str) -> object:
    for factory in FACTORIES:
        if data.startswith(f"{factory.__prefix__}:"):
            return factory.unpack(data)
    raise AssertionError(data)


@pytest.mark.parametrize("locale", list(Locale))
def test_every_type_has_a_template(locale: Locale) -> None:
    assert set(TEMPLATES[locale]) == set(NotificationType)
    assert {path.stem for path in FIXTURES.glob("*.json")} == set(NotificationType)


@pytest.mark.parametrize("locale", list(Locale))
@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_fixture_renders(path: Path, locale: Locale) -> None:
    payload = fixture(path)
    rendered = render(payload, PUBLIC_URL, locale=locale)
    assert rendered.text.strip()
    assert len(rendered.text) <= MAX_TEXT
    assert_html(rendered.text)
    api = [action for action in payload.actions if isinstance(action, ApiAction)]
    packed = [button.callback_data for button in buttons(rendered) if button.callback_data]
    for data in packed:
        assert len(data.encode()) <= MAX_CALLBACK_BYTES, data
        unpack(data)
    assert [
        (item.key, item.entity_id)
        for item in map(unpack, packed)
        if isinstance(item, ActionCallback)
    ] == [(action.key, action.entity_id) for action in api]
    assert [button.text for button in buttons(rendered) if button.callback_data][: len(api)] == [
        TEXTS[locale].api_labels[action.key] for action in api
    ]
    urls = [button.url for button in buttons(rendered) if button.url]
    assert all(url.startswith(PUBLIC_URL) for url in urls)
    assert "Open in hmm" not in rendered.text


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_local_public_url_moves_links_into_text(path: Path) -> None:
    payload = fixture(path)
    local = "http://localhost:8000"
    rendered = render(payload, local)
    assert all(button.url is None for button in buttons(rendered))
    assert f'<a href="{local}' not in rendered.text
    assert_html(rendered.text)


def test_native_flows_replace_links() -> None:
    expected = {
        "help_request_match": HelpCallback,
        "help_request_direct": HelpCallback,
        "mentorship_idle": CheckinCallback,
        "mentorship_stalled": CheckinCallback,
        "mentorship_close_proposed": CloseCallback,
        "attendance_confirm_digest": ConfirmCallback,
        "contributions_confirm_digest": ConfirmCallback,
    }
    for stem, factory in expected.items():
        rendered = render(fixture(FIXTURES / f"{stem}.json"), PUBLIC_URL)
        kinds = {
            type(unpack(button.callback_data))
            for button in buttons(rendered)
            if button.callback_data
        }
        assert factory in kinds, stem


def test_unknown_type_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = EventCancelledPayload(
        link="/events", event_id=uuid4(), event_title="Event", reason="Cancelled"
    )
    monkeypatch.delitem(TEMPLATES[Locale.EN], NotificationType.EVENT_CANCELLED)
    rendered = render(payload, PUBLIC_URL)
    assert rendered.text == TEXTS[Locale.EN].fallback
    assert rendered.markup is None


def test_user_text_is_escaped() -> None:
    payload = EventCancelledPayload(
        link="/events",
        event_id=uuid4(),
        event_title="<b>Hack</b> & <script>",
        reason="a < b > c",
    )
    rendered = render(payload, PUBLIC_URL)
    assert "&lt;b&gt;Hack&lt;/b&gt; &amp; &lt;script&gt;" in rendered.text
    assert "a &lt; b &gt; c" in rendered.text
    assert_html(rendered.text)


def test_subject_links_inline_only_on_https() -> None:
    payload = EventCancelledPayload(
        link="/events/1", event_id=uuid4(), event_title="Hack", reason="Venue closed"
    )
    assert '<a href="https://hmm.test/events/1"><b>Hack</b></a>' in render(payload, PUBLIC_URL).text
    assert render(payload, "http://localhost:8000").text.startswith("❌ <b>Hack</b>")


def test_times_use_user_timezone_and_relative_hint() -> None:
    payload = fixture(FIXTURES / "party_invite.json")
    assert isinstance(payload, PartyInvitePayload)
    now = payload.expires_at - timedelta(days=3, hours=1)
    text = render(payload, PUBLIC_URL, now=now, timezone="Asia/Tokyo").text
    local = payload.expires_at.astimezone(ZoneInfo("Asia/Tokyo"))
    assert f"{local:%a %d %b %H:%M} JST (in 3 days)" in text
    assert "(in " not in render(payload, PUBLIC_URL).text


def test_quiet_types_are_silent() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        payload = fixture(path)
        assert render(payload, PUBLIC_URL).silent is (payload.type in SILENT)


def test_locale_tables_match() -> None:
    english, russian = TEXTS[Locale.EN], TEXTS[Locale.RU]
    for field in fields(Texts):
        value = getattr(english, field.name)
        if isinstance(value, dict):
            assert set(value) == set(getattr(russian, field.name)), field.name
    assert set(english.commands) == {command.name for command in COMMANDS}
    for table, enum in (
        (english.event_kinds, EventKind),
        (english.event_formats, EventFormat),
        (english.mentorship_kinds, MentorshipKind),
        (english.mentorship_statuses, MentorshipStatus),
        (english.outcomes, MentorshipOutcome),
        (english.team_statuses, TeamStatus),
        (english.restriction_kinds, RestrictionKind),
        (english.alert_kinds, AlertKind),
        (english.api_labels, ActionKey),
        (english.action_done, ActionKey),
        (english.menu, MenuItem),
    ):
        assert set(table) == set(enum)


def test_russian_plurals() -> None:
    noun = TEXTS[Locale.RU].team
    picks = {count: noun.pick(count) for count in (1, 2, 5, 11, 12, 21, 22, 25, 111)}
    assert picks == {
        1: "команда",
        2: "команды",
        5: "команд",
        11: "команд",
        12: "команд",
        21: "команда",
        22: "команды",
        25: "команд",
        111: "команд",
    }


def test_russian_dates_and_relative_hint() -> None:
    payload = fixture(FIXTURES / "party_invite.json")
    assert isinstance(payload, PartyInvitePayload)
    now = payload.expires_at - timedelta(days=3, hours=1)
    text = render(payload, PUBLIC_URL, locale=Locale.RU, now=now, timezone="Europe/Moscow").text
    local = payload.expires_at.astimezone(ZoneInfo("Europe/Moscow"))
    weekday = TEXTS[Locale.RU].weekdays[local.weekday()]
    month = TEXTS[Locale.RU].months[local.month - 1]
    assert f"{weekday} {local:%d} {month} {local:%H:%M} MSK (через 3 дня)" in text
    assert text.startswith("👥")


def test_telegram_locale() -> None:
    assert telegram_locale("ru") is Locale.RU
    assert telegram_locale("ru-RU") is Locale.RU
    assert telegram_locale("en") is Locale.EN
    assert telegram_locale("uk") is Locale.EN
    assert telegram_locale(None) is Locale.EN


async def test_throttle_spaces_sends() -> None:
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(global_interval=0.04, chat_interval=1.0, clock=lambda: now[0], sleep=sleep)
    await throttle.wait(1)
    await throttle.wait(2)
    await throttle.wait(1)
    assert slept == [pytest.approx(0.04), pytest.approx(0.96)]
    now[0] += 5
    await throttle.wait(1)
    assert len(slept) == 2
