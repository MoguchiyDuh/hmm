import re
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram.types import InlineKeyboardButton

from hmm.bot import templates
from hmm.bot.callbacks import (
    ActionCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    HelpCallback,
)
from hmm.bot.delivery import Throttle
from hmm.bot.templates import API_LABELS, TEMPLATES, render
from hmm.notifications.payloads import (
    ApiAction,
    EventCancelledPayload,
    Notification,
    NotificationPayload,
    NotificationType,
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


def test_every_type_has_a_template() -> None:
    assert set(TEMPLATES) == set(NotificationType)
    assert {path.stem for path in FIXTURES.glob("*.json")} == set(NotificationType)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_fixture_renders(path: Path) -> None:
    payload = fixture(path)
    rendered = render(payload, PUBLIC_URL)
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
        API_LABELS[action.key] for action in api
    ]
    urls = [button.url for button in buttons(rendered) if button.url]
    assert all(url.startswith(PUBLIC_URL) for url in urls)
    assert f"{PUBLIC_URL}{payload.link}" in urls


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_local_public_url_moves_links_into_text(path: Path) -> None:
    payload = fixture(path)
    local = "http://localhost:8000"
    rendered = render(payload, local)
    assert all(button.url is None for button in buttons(rendered))
    assert f'<a href="{local}{payload.link}">' in rendered.text
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
    monkeypatch.delitem(TEMPLATES, NotificationType.EVENT_CANCELLED)
    rendered = render(payload, PUBLIC_URL)
    assert rendered.text == "🔔 You have a new notification."
    assert [button.url for button in buttons(rendered)] == [f"{PUBLIC_URL}/events"]


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
