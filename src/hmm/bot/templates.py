from dataclasses import dataclass
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hmm.bot import templates_en, templates_ru
from hmm.bot.callbacks import (
    ActionCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    HelpCallback,
)
from hmm.bot.fmt import View, clip, current_locale, humanize, tr, viewing
from hmm.bot.i18n import Template
from hmm.common import ConfirmationFactType, Locale
from hmm.notifications.payloads import (
    ApiAction,
    AttendanceConfirmDigestPayload,
    ContributionsConfirmDigestPayload,
    HelpRequestDirectPayload,
    HelpRequestMatchPayload,
    LinkAction,
    MentorshipCloseProposedPayload,
    MentorshipIdlePayload,
    MentorshipStalledPayload,
    NotificationPayload,
    NotificationType,
)

TEMPLATES: dict[Locale, dict[NotificationType, Template]] = {
    Locale.EN: templates_en.TEMPLATES,
    Locale.RU: templates_ru.TEMPLATES,
}

CONFIRM_LINKS = frozenset(
    {"notifications.actions.confirm_attendees", "notifications.actions.confirm_lead"}
)

SILENT = frozenset(
    {
        NotificationType.INVITE_ACCEPTED,
        NotificationType.INVITE_DECLINED,
        NotificationType.INVITE_EXPIRED,
        NotificationType.INVITE_CLOSED,
        NotificationType.TEAM_MEMBER_JOINED,
        NotificationType.TEAM_MEMBER_LEFT,
        NotificationType.PROJECT_MEMBER_JOINED,
        NotificationType.PROJECT_MEMBER_LEFT,
        NotificationType.EVENT_CHANGED,
        NotificationType.HELP_OFFER_CLOSED,
        NotificationType.REVIEW_RECEIVED,
        NotificationType.REVIEW_REMOVED,
        NotificationType.SCORE_CHANGED,
        NotificationType.SCORES_REVIEWED,
        NotificationType.ADMIN_DIGEST,
    }
)

MAX_CONFIRM_BUTTONS = 20


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    markup: InlineKeyboardMarkup | None
    silent: bool = False


def native_button(action: LinkAction, payload: NotificationPayload) -> InlineKeyboardButton | None:
    label = tr().link_labels.get(action.label_key, "")
    match action.label_key, payload:
        case "notifications.actions.help", HelpRequestMatchPayload() | HelpRequestDirectPayload():
            data = HelpCallback(request_id=payload.help_request_id).pack()
        case (
            "notifications.actions.check_in",
            MentorshipIdlePayload() | MentorshipStalledPayload(),
        ):
            data = CheckinCallback(mentorship_id=payload.mentorship_id).pack()
        case "notifications.actions.close_with_outcome", MentorshipCloseProposedPayload():
            data = CloseCallback(mentorship_id=payload.mentorship_id).pack()
        case _:
            return None
    return InlineKeyboardButton(text=label, callback_data=data)


def confirm_buttons(payload: NotificationPayload) -> list[InlineKeyboardButton]:
    match payload:
        case AttendanceConfirmDigestPayload():
            return [
                InlineKeyboardButton(
                    text=f"☐ {clip(attendee.display_name, 40)}",
                    callback_data=ConfirmCallback(
                        fact_type=ConfirmationFactType.ATTENDANCE, fact_id=attendee.attendance_id
                    ).pack(),
                )
                for attendee in payload.attendees[:MAX_CONFIRM_BUTTONS]
            ]
        case ContributionsConfirmDigestPayload():
            return [
                InlineKeyboardButton(
                    text=tr().confirm_lead.format(clip(payload.lead.display_name, 40)),
                    callback_data=ConfirmCallback(
                        fact_type=ConfirmationFactType.PROJECT_MEMBER,
                        fact_id=payload.project_member_id,
                    ).pack(),
                )
            ]
        case _:
            return []


def link_buttons(payload: NotificationPayload) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for action in payload.actions:
        if not isinstance(action, LinkAction) or native_button(action, payload) is not None:
            continue
        if action.label_key in CONFIRM_LINKS and not (
            isinstance(payload, AttendanceConfirmDigestPayload)
            and len(payload.attendees) > MAX_CONFIRM_BUTTONS
        ):
            continue
        label = (
            tr().link_labels.get(action.label_key)
            or humanize(action.label_key.rsplit(".", 1)[-1]).capitalize()
        )
        links.append((action.url, label))
    open_label = tr().open_labels.get(payload.type)
    if open_label is not None and all(path != payload.link for path, _ in links):
        links.append((payload.link, open_label))
    return links


def render(
    payload: NotificationPayload,
    public_url: str,
    *,
    locale: Locale = Locale.EN,
    now: datetime | None = None,
    timezone: str | None = None,
) -> Rendered:
    view = View(public_url=public_url, now=now, timezone=timezone, link=payload.link, locale=locale)
    with viewing(view):
        return build(payload, public_url)


def build(payload: NotificationPayload, public_url: str) -> Rendered:
    texts = tr()
    body = TEMPLATES[current_locale()].get(payload.type)
    text = body(payload) if body is not None else texts.fallback
    api_row = [
        InlineKeyboardButton(
            text=texts.api_labels[action.key],
            callback_data=ActionCallback(key=action.key, entity_id=action.entity_id).pack(),
        )
        for action in payload.actions
        if isinstance(action, ApiAction)
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [button]
        for action in payload.actions
        if isinstance(action, LinkAction) and (button := native_button(action, payload))
    ]
    rows.extend([button] for button in confirm_buttons(payload))
    if public_url.startswith("https://"):
        rows.extend(
            [InlineKeyboardButton(text=label, url=f"{public_url}{path}")]
            for path, label in link_buttons(payload)
        )
    keyboard = ([api_row] if api_row else []) + rows
    return Rendered(
        text=text,
        markup=InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None,
        silent=payload.type in SILENT,
    )
