from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hmm.bot.callbacks import MenuItem
from hmm.common import (
    AlertSeverity,
    EnrollmentStatus,
    ErrorCode,
    EventFormat,
    EventKind,
    HelpRequestFormat,
    Locale,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    RestrictionKind,
    TeamStatus,
)
from hmm.notifications.payloads import ActionKey, AlertKind, NotificationType

LINK_PATH = "/settings#telegram"


@dataclass(frozen=True, slots=True)
class Noun:
    forms: tuple[str, ...]

    def pick(self, count: int) -> str:
        if len(self.forms) == 2:
            return self.forms[0] if count == 1 else self.forms[1]
        tail, tens = count % 10, count % 100
        if tail == 1 and tens != 11:
            return self.forms[0]
        if 2 <= tail <= 4 and not 12 <= tens <= 14:
            return self.forms[1]
        return self.forms[2]


@dataclass(frozen=True, slots=True, kw_only=True)
class Texts:
    weekdays: tuple[str, str, str, str, str, str, str]
    months: tuple[str, str, str, str, str, str, str, str, str, str, str, str]
    in_time: str
    ago_time: str
    just_now: str
    minute: Noun
    hour: Noun
    day: Noun
    person: Noun
    team: Noun
    member: Noun
    hackathon: Noun
    shipped_project: Noun
    completed_mentorship: Noun
    and_more: str
    span_days: str

    cancel: str
    cancelled: str
    nothing_to_cancel: str
    unknown: str
    fallback: str
    help_title: str
    usage: str
    commands: dict[str, tuple[str, str]]
    menu: dict[MenuItem, str]

    linked: str
    linked_next: str
    link_intro: str
    link_how: str
    greeting: str
    unlinked: str
    settings_text: str
    settings_button: str
    language_prompt: str
    language_set: str

    no_events: str
    events_title: str
    register_by: str
    teams_form: str
    teams_forming: str
    teams_formed: str
    queue_line: str
    queue_only: str
    your_team: str
    team_size: str
    going_count: str
    you_are_going: str
    op_going: str
    op_enroll: str
    op_party: str
    op_withdraw: str
    all_events: str
    invite_teammates: str
    marked_going: str
    enrolled_notice: str
    left_queue: str
    party_created: str
    enrolled_for: str
    the_event: str

    no_teams: str
    teams_title: str
    accept_place_by: str
    accept: str
    decline: str

    no_invites: str
    invites_title: str
    invite_line: str
    someone: str
    invite_team: str
    a_team: str
    invite_party: str
    a_party: str
    expires: str

    ask_pick_area: str
    ask_topic_head: str
    ask_topic_hint: str
    ask_goal_head: str
    ask_goal_hint: str
    request_created: str
    request_created_next: str

    no_mentor_offers: str
    setup_mentoring: str
    no_requests: str
    requests_title: str
    help_button: str
    help_prompt_head: str
    help_prompt_hint: str
    offer_sent: str
    cant_offer: str

    no_mentorships: str
    mentorships_title: str
    last_checkin: str
    no_checkins: str
    mentoring_with: str
    learning_from: str
    check_in: str
    close: str
    checkin_prompt: str
    cant_checkin: str
    checkin_done: str
    close_prompt: str
    cant_close: str
    mentorship_closed: str

    confirm_title: str
    confirm_hint: str
    nothing_to_confirm: str
    confirmed: str
    member_fallback: str
    confirm_lead: str

    xp: str
    activity: str
    top_areas: str
    teams_line: str
    profile_empty: str

    dnd_off: str
    dnd_forever: str
    dnd_until: str
    dnd_help: str

    invalid_field: str

    action_done: dict[ActionKey, str]
    api_labels: dict[ActionKey, str]
    link_labels: dict[str, str]
    open_labels: dict[NotificationType, str]
    errors: dict[ErrorCode, str]
    validation: dict[str, str]
    field_names: dict[str, str]
    event_kinds: dict[EventKind, str]
    event_formats: dict[EventFormat, str]
    event_fields: dict[str, str]
    help_formats: dict[HelpRequestFormat, str]
    mentorship_kinds: dict[MentorshipKind, str]
    mentorship_statuses: dict[MentorshipStatus, str]
    outcomes: dict[MentorshipOutcome, str]
    enrollment: dict[EnrollmentStatus, str]
    team_statuses: dict[TeamStatus, str]
    restriction_kinds: dict[RestrictionKind, str]
    alert_kinds: dict[AlertKind, str]


LANGUAGE_NAMES: dict[Locale, str] = {
    Locale.EN: "🇬🇧 English",
    Locale.RU: "🇷🇺 Русский",
}


def telegram_locale(language_code: str | None) -> Locale:
    if language_code and language_code.split("-", 1)[0].lower() == Locale.RU:
        return Locale.RU
    return Locale.EN


SEVERITY_ICONS: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: "🔹",
    AlertSeverity.WARNING: "⚠️",
    AlertSeverity.CRITICAL: "🚨",
}

type Template = Callable[[Any], str]
type Register = Callable[[NotificationType], Callable[[Template], Template]]


def registrar() -> tuple[dict[NotificationType, Template], Register]:
    table: dict[NotificationType, Template] = {}

    def template(kind: NotificationType) -> Callable[[Template], Template]:
        def register(body: Template) -> Template:
            table[kind] = body
            return body

        return register

    return table, template
