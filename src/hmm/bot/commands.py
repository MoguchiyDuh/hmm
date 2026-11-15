from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.attendance import events as attendance_events
from hmm.attendance.schemas import SetAttendanceBody
from hmm.bot.callbacks import (
    ActionCallback,
    AskAreaCallback,
    CancelCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    EventCallback,
    EventOp,
    HelpCallback,
    LanguageCallback,
    MenuCallback,
    MenuItem,
    OutcomeCallback,
)
from hmm.bot.fmt import (
    bold,
    clip,
    current_locale,
    day,
    esc,
    headcount,
    italic,
    lines,
    localized,
    plural,
    quote,
    span,
    subject,
    tr,
    web_url,
    when,
)
from hmm.bot.i18n import LANGUAGE_NAMES, LINK_PATH, telegram_locale
from hmm.bot.runtime import BotRuntime, Reply
from hmm.catalog import views as catalog_views
from hmm.common import (
    AttendanceStatus,
    ConfirmationFactType,
    ErrorCode,
    EventPhase,
    FormationPhase,
    HelpRequestFormat,
    HelpRequestStatus,
    Locale,
    MemberState,
    MentorshipOutcome,
    MentorshipStatus,
    TeamStatus,
)
from hmm.feed import confirmations as feed_confirmations
from hmm.feed import history as feed_history
from hmm.formation import parties as formation_parties
from hmm.formation import queue as formation_queue
from hmm.formation.schemas import CreateEnrollmentBody, Formation
from hmm.identity import service as identity
from hmm.mentorship import help_offers, mentor_offers
from hmm.mentorship import mentorships as mentorship_mentorships
from hmm.mentorship import requests as mentorship_requests
from hmm.mentorship.schemas import (
    CloseMentorshipBody,
    CreateHelpOfferBody,
    CreateHelpRequestBody,
    CreateMentorshipCheckinBody,
    HelpRequestAction,
    MentorshipAction,
)
from hmm.notifications.actions import run_action
from hmm.notifications.payloads import ActionKey
from hmm.platform.errors import ProblemError
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import EventAction
from hmm.profiles import service as profiles
from hmm.profiles.schemas import UpdateProfileBody
from hmm.projects import invites as project_invites
from hmm.projects import records as project_records
from hmm.scores import service as scores
from hmm.scores.snapshot import member_metrics


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    listed: bool = True

    def args(self) -> str:
        return tr().commands[self.name][0]

    def text(self) -> str:
        return tr().commands[self.name][1]


COMMANDS: tuple[Command, ...] = (
    Command("events"),
    Command("team"),
    Command("invites"),
    Command("profile"),
    Command("ask"),
    Command("requests"),
    Command("mentorships"),
    Command("confirm"),
    Command("dnd"),
    Command("settings"),
    Command("language"),
    Command("unlink"),
    Command("help"),
    Command("event", listed=False),
    Command("enroll", listed=False),
)

MENU_COMMANDS = tuple(command for command in COMMANDS if command.listed)


def bot_commands(locale: Locale) -> list[BotCommand]:
    with localized(locale):
        return [
            BotCommand(command=command.name, description=command.text())
            for command in MENU_COMMANDS
        ]


CURRENT_TEAM_STATUSES = frozenset({TeamStatus.FORMED, TeamStatus.ACTIVE})
LIVE_MENTORSHIP_STATUSES = frozenset(
    {MentorshipStatus.ACTIVE, MentorshipStatus.STALLED, MentorshipStatus.CLOSING}
)
LIST_LIMIT = 10
DND_MAX_DAYS = 365
BUTTON_TITLE = 40
AREA_COLUMNS = 2


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def numbered(index: int, total: int, label: str) -> str:
    return f"{index} · {label}" if total > 1 else label


def marker(index: int, total: int) -> str:
    return f"{index}." if total > 1 else "•"


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup | None:
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def with_web(text: str, rows: list[list[InlineKeyboardButton]], path: str, label: str) -> Reply:
    url = web_url(path)
    if url is not None:
        rows = [*rows, [InlineKeyboardButton(text=label, url=url)]]
    return Reply(text, keyboard(rows))


def cancel_button() -> InlineKeyboardButton:
    return button(tr().cancel, CancelCallback().pack())


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cancel_button()]])


def prompt(text: str) -> Reply:
    return Reply(text, cancel_markup())


def menu_markup() -> InlineKeyboardMarkup:
    items = list(tr().menu.items())
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(label, MenuCallback(item=item).pack())
                for item, label in items[start : start + 2]
            ]
            for start in range(0, len(items), 2)
        ]
    )


def usage(command: str) -> Reply:
    args, text = tr().commands[command]
    return Reply(tr().usage.format(name=command, args=esc(args), text=esc(text)))


def help_text() -> str:
    return lines(
        bold(tr().help_title),
        *(
            f"/{command.name}{esc(command.args())} — {esc(command.text())}"
            for command in MENU_COMMANDS
        ),
    )


def language_markup() -> InlineKeyboardMarkup:
    current = current_locale()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    f"✓ {name}" if locale == current else name,
                    LanguageCallback(locale=locale).pack(),
                )
                for locale, name in LANGUAGE_NAMES.items()
            ]
        ]
    )


def language_menu() -> Reply:
    return Reply(tr().language_prompt, language_markup())


async def set_language(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    locale: Locale,
) -> Reply:
    await profiles.update_profile(session, runtime.jobs, actor, UpdateProfileBody(locale=locale))
    with localized(locale):
        return Reply(tr().language_set, menu_markup(), locale=locale)


async def resolve_event_id(session: AsyncSession, value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        event_id = await session.scalar(select(models.Event.id).where(models.Event.slug == value))
    if event_id is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return event_id


async def names(session: AsyncSession, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(models.User.id, models.User.display_name).where(models.User.id.in_(user_ids))
    )
    return {user_id: name for user_id, name in rows}


async def area_names(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(select(models.Area.key, models.Area.name))
    return {key: name for key, name in rows}


async def link_account(
    runtime: BotRuntime,
    session: AsyncSession,
    now: datetime,
    *,
    code: str,
    telegram_user_id: int,
    chat_id: int,
    username: str | None,
    language_code: str | None = None,
) -> Reply:
    actor = await identity.link_telegram(session, code, telegram_user_id, chat_id, username, now)
    detected = telegram_locale(language_code)
    if Locale(actor.locale) == Locale.EN and detected != Locale.EN:
        actor = await profiles.update_profile(
            session, runtime.jobs, actor, UpdateProfileBody(locale=detected)
        )
    locale = Locale(actor.locale)
    with localized(locale):
        texts = tr()
        return Reply(
            lines(texts.linked.format(name=bold(actor.display_name)), texts.linked_next),
            menu_markup(),
            locale=locale,
        )


async def linked_name(session: AsyncSession, telegram_user_id: int) -> str | None:
    return await session.scalar(
        select(models.User.display_name)
        .join(models.TelegramLink, models.TelegramLink.user_id == models.User.id)
        .where(models.TelegramLink.telegram_user_id == telegram_user_id)
    )


def link_instructions(runtime: BotRuntime) -> Reply:
    return Reply(
        lines(
            tr().link_intro,
            tr().link_how.format(url=esc(runtime.url(LINK_PATH))),
        )
    )


async def greet(runtime: BotRuntime, session: AsyncSession, telegram_user_id: int) -> Reply:
    name = await linked_name(session, telegram_user_id)
    if name is None:
        return link_instructions(runtime)
    return Reply(tr().greeting.format(name=bold(name)), menu_markup())


async def unknown(runtime: BotRuntime, session: AsyncSession, telegram_user_id: int) -> Reply:
    if await linked_name(session, telegram_user_id) is None:
        return link_instructions(runtime)
    return Reply(tr().unknown)


async def unlink(session: AsyncSession, telegram_user_id: int) -> Reply:
    await identity.unlink_telegram_account(session, telegram_user_id)
    return Reply(tr().unlinked)


def settings_reply() -> Reply:
    return with_web(tr().settings_text, [], "/settings#notifications", tr().settings_button)


async def events(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    page = await catalog_views.list_events(
        session,
        CursorParams(limit=LIST_LIMIT),
        actor,
        now,
        phase=EventPhase.REGISTRATION_OPEN,
    )
    if not page.items:
        return Reply(tr().no_events)
    parts: list[str] = [bold(tr().events_title)]
    for event in page.items:
        parts.append(
            lines(
                f"• {subject(event.title, f'/events/{event.id}')} · "
                f"{esc(tr().event_kinds[event.kind])}",
                f"  {span(event.starts_at, event.ends_at, event.timezone)}",
                "  " + tr().register_by.format(when(event.registration_deadline, event.timezone))
                if event.registration_deadline
                else None,
            )
        )
    rows = [
        [
            button(
                clip(event.title, BUTTON_TITLE),
                EventCallback(op=EventOp.SHOW, event_id=event.id).pack(),
            )
        ]
        for event in page.items
    ]
    return Reply("\n\n".join(parts), keyboard(rows))


def formation_lines(formation: Formation | None, timezone: str) -> list[str | None]:
    if formation is None:
        return []
    projected = len(formation.projection.new_teams) + len(formation.projection.fills)
    queued = formation.counts.get("queued", 0)
    texts = tr()
    match formation.phase:
        case FormationPhase.OPEN:
            timing = texts.teams_form.format(when(formation.formation_at, timezone))
        case FormationPhase.FORMING:
            timing = texts.teams_forming
        case _:
            timing = texts.teams_formed
    status = formation.my_enrollment.status if formation.my_enrollment else None
    team = formation.my_team
    queue = None
    if queued and formation.phase == FormationPhase.OPEN:
        queue = (
            texts.queue_line.format(people=headcount(queued), teams=plural(projected, texts.team))
            if projected
            else texts.queue_only.format(headcount(queued))
        )
    return [
        timing,
        queue,
        texts.enrollment.get(status) if status else None,
        texts.your_team.format(subject(team.name, f"/projects/{team.id}")) if team else None,
    ]


async def event_card(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    event_id: UUID,
) -> Reply:
    event = await catalog_views.visible_event(session, event_id, actor)
    detail = await catalog_views.event_detail(session, event, actor, now)
    tz = detail.timezone
    texts = tr()
    facts = [
        texts.event_formats[detail.format] if detail.format else None,
        texts.team_size.format(detail.team_size_min, detail.team_size_max)
        if detail.team_size_min and detail.team_size_max
        else None,
        texts.going_count.format(detail.attendance_counts.going)
        if detail.attendance_counts.going
        else None,
    ]
    going = (
        detail.my_attendance is not None and detail.my_attendance.status == AttendanceStatus.GOING
    )
    text = lines(
        f"{subject(detail.title, f'/events/{detail.id}')} · {esc(texts.event_kinds[detail.kind])}",
        span(detail.starts_at, detail.ends_at, tz),
        texts.register_by.format(when(detail.registration_deadline, tz))
        if detail.registration_deadline
        else None,
        esc(" · ".join(fact for fact in facts if fact)) or None,
        quote(detail.summary, 300),
        texts.you_are_going if going else None,
        *formation_lines(detail.formation, tz),
    )
    ops = [
        (EventAction.GOING, EventOp.GOING, texts.op_going),
        (EventAction.ENROLL, EventOp.ENROLL, texts.op_enroll),
        (EventAction.CREATE_PARTY, EventOp.PARTY, texts.op_party),
        (EventAction.WITHDRAW, EventOp.WITHDRAW, texts.op_withdraw),
    ]
    actions = [
        button(label, EventCallback(op=op, event_id=detail.id).pack())
        for action, op, label in ops
        if action in detail.allowed_actions and not (op == EventOp.GOING and going)
    ]
    rows = [actions[start : start + 2] for start in range(0, len(actions), 2)]
    back = EventCallback(op=EventOp.LIST, event_id=detail.id).pack()
    rows.append([button(texts.all_events, back)])
    return Reply(text, keyboard(rows))


async def event_op(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    op: EventOp,
    event_id: UUID,
) -> Reply:
    match op:
        case EventOp.LIST:
            return await events(runtime, session, actor, now)
        case EventOp.SHOW:
            return await event_card(runtime, session, actor, now, event_id)
        case EventOp.GOING:
            await attendance_events.set_attendance(
                session,
                runtime.jobs,
                event_id,
                SetAttendanceBody(status=AttendanceStatus.GOING),
                actor,
                now,
            )
            notice = tr().marked_going
        case EventOp.ENROLL:
            await formation_queue.enroll(session, event_id, CreateEnrollmentBody(), actor, now)
            notice = tr().enrolled_notice
        case EventOp.WITHDRAW:
            await formation_queue.withdraw_enrollment(session, event_id, actor, now)
            notice = tr().left_queue
        case EventOp.PARTY:
            await formation_parties.create_party(session, event_id, actor, now)
            notice = tr().party_created
    card = replace(await event_card(runtime, session, actor, now, event_id), toast=notice)
    if op != EventOp.PARTY:
        return card
    url = web_url(f"/events/{event_id}")
    if url is None or card.markup is None:
        return card
    invite = InlineKeyboardButton(text=tr().invite_teammates, url=url)
    rows = [[invite], *card.markup.inline_keyboard]
    return replace(card, markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def enroll(session: AsyncSession, actor: models.User, now: datetime, event_id: UUID) -> Reply:
    await formation_queue.enroll(session, event_id, CreateEnrollmentBody(), actor, now)
    event = await session.get(models.Event, event_id)
    formation_at = await session.scalar(
        select(models.EventFormation.formation_at).where(models.EventFormation.event_id == event_id)
    )
    title = subject(event.title, f"/events/{event_id}") if event else tr().the_event
    timezone = event.timezone if event else None
    return Reply(
        lines(
            tr().enrolled_for.format(title),
            tr().teams_form.format(when(formation_at, timezone)) if formation_at else None,
        )
    )


async def teams(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    projects = [
        project
        for project in await project_records.list_my_projects(session, actor, None)
        if project.team_status in CURRENT_TEAM_STATUSES
    ]
    if not projects:
        return Reply(tr().no_teams)
    memberships = {
        row.project_id: row
        for row in await session.scalars(
            select(models.ProjectMember).where(
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.project_id.in_([project.id for project in projects]),
                models.ProjectMember.left_at.is_(None),
            )
        )
    }
    event_ids = {project.event_id for project in projects if project.event_id}
    events_by_id = {
        event.id: event
        for event in await session.scalars(
            select(models.Event).where(models.Event.id.in_(event_ids))
        )
    }
    texts = tr()
    parts: list[str] = [bold(texts.teams_title)]
    rows: list[list[InlineKeyboardButton]] = []
    total = len(projects)
    for index, project in enumerate(projects, 1):
        event = events_by_id.get(project.event_id) if project.event_id else None
        member = memberships.get(project.id)
        accept_by = (
            member.accept_by
            if member is not None
            and member.state == MemberState.PROPOSED
            and member.accept_by is not None
            and member.accept_by > now
            else None
        )
        parts.append(
            lines(
                f"• {subject(project.name, f'/projects/{project.id}')}"
                + (f" · {esc(event.title)}" if event else ""),
                "  "
                + texts.accept_place_by.format(when(accept_by, event.timezone if event else None))
                if accept_by
                else None,
            )
        )
        if accept_by:
            rows.append(
                [
                    button(
                        numbered(index, total, texts.accept),
                        ActionCallback(key=ActionKey.PLACEMENT_ACCEPT, entity_id=project.id).pack(),
                    ),
                    button(
                        numbered(index, total, texts.decline),
                        ActionCallback(
                            key=ActionKey.PLACEMENT_DECLINE, entity_id=project.id
                        ).pack(),
                    ),
                ]
            )
    return Reply("\n".join(parts), keyboard(rows))


async def invites(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    pending = await project_invites.my_invites(session, actor, "pending")
    if not pending:
        return Reply(tr().no_invites)
    inviters = await names(session, {row.invited_by for row in pending if row.invited_by})
    texts = tr()
    parts: list[str] = [bold(texts.invites_title)]
    rows: list[list[InlineKeyboardButton]] = []
    total = len(pending)
    for index, invite in enumerate(pending, 1):
        if invite.project_id is not None:
            project = await session.get(models.Project, invite.project_id)
            target = (
                texts.invite_team.format(subject(project.name, f"/projects/{project.id}"))
                if project
                else texts.a_team
            )
        else:
            event = await session.get(models.Event, invite.event_id) if invite.event_id else None
            target = (
                texts.invite_party.format(subject(event.title, f"/events/{event.id}"))
                if event
                else texts.a_party
            )
        inviter = inviters.get(invite.invited_by) if invite.invited_by else None
        parts.append(
            lines(
                f"{marker(index, total)} "
                + texts.invite_line.format(
                    inviter=bold(inviter) if inviter else texts.someone, target=target
                ),
                texts.expires.format(when(invite.expires_at)),
            )
        )
        rows.append(
            [
                button(
                    numbered(index, total, texts.accept),
                    ActionCallback(key=ActionKey.INVITE_ACCEPT, entity_id=invite.id).pack(),
                ),
                button(
                    numbered(index, total, texts.decline),
                    ActionCallback(key=ActionKey.INVITE_DECLINE, entity_id=invite.id).pack(),
                ),
            ]
        )
    return Reply("\n\n".join(parts), keyboard(rows))


async def ask_areas(session: AsyncSession) -> Reply:
    rows = list(
        await session.execute(
            select(models.Area.key, models.Area.name)
            .where(models.Area.archived_at.is_(None))
            .order_by(models.Area.sort, models.Area.key)
        )
    )
    buttons = [button(name, AskAreaCallback(area_key=key).pack()) for key, name in rows]
    grid = [buttons[start : start + AREA_COLUMNS] for start in range(0, len(buttons), AREA_COLUMNS)]
    grid.append([cancel_button()])
    return Reply(tr().ask_pick_area, keyboard(grid))


async def ask_topic(session: AsyncSession, area_key: str) -> tuple[Reply, dict[str, str]]:
    area = await mentor_offers.load_area(session, area_key)
    return (
        prompt(
            lines(
                tr().ask_topic_head.format(bold(area.name)),
                tr().ask_topic_hint,
            )
        ),
        {"area_key": area.key},
    )


async def start_ask(
    session: AsyncSession, area_key: str, topic: str
) -> tuple[Reply, dict[str, str]]:
    area = await mentor_offers.load_area(session, area_key)
    CreateHelpRequestBody(area_key=area.key, topic=topic, goal="-", format=HelpRequestFormat.ANY)
    return (
        prompt(
            lines(
                tr().ask_goal_head.format(topic=bold(topic), area=bold(area.name)),
                tr().ask_goal_hint,
            )
        ),
        {"area_key": area.key, "topic": topic},
    )


async def create_help_request(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    *,
    area_key: str,
    topic: str,
    goal: str,
) -> Reply:
    body = CreateHelpRequestBody(
        area_key=area_key, topic=topic, goal=goal, format=HelpRequestFormat.ANY
    )
    request = await mentorship_requests.create_help_request(session, runtime.jobs, actor, body, now)
    return Reply(
        lines(
            tr().request_created.format(subject(request.topic, f"/help-requests/{request.id}")),
            tr().request_created_next,
        )
    )


async def requests(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    offers = await mentor_offers.mentor_offers_view(session, actor)
    area_keys = [offer.area_key for offer in offers.offers if offer.active]
    if not area_keys:
        return with_web(tr().no_mentor_offers, [], "/mentorship", tr().setup_mentoring)
    areas = await area_names(session)
    found: list[tuple[UUID, str, str | None, bool]] = []
    for area_key in area_keys:
        page = await mentorship_requests.list_help_requests(
            session,
            actor,
            CursorParams(limit=LIST_LIMIT),
            area_key=area_key,
            request_status=HelpRequestStatus.OPEN,
            now=now,
        )
        for request in page.items:
            if len(found) >= LIST_LIMIT or any(item[0] == request.id for item in found):
                continue
            area = areas.get(request.area_key, request.area_key)
            found.append(
                (
                    request.id,
                    f"{bold(request.topic)} · {esc(area)} · {esc(request.learner.display_name)}",
                    quote(request.goal, 200),
                    HelpRequestAction.OFFER_HELP in request.allowed_actions,
                )
            )
    if not found:
        return Reply(tr().no_requests)
    total = len(found)
    rows = [
        [
            button(
                numbered(index, total, tr().help_button),
                HelpCallback(request_id=request_id).pack(),
            )
        ]
        for index, (request_id, _, _, can_help) in enumerate(found, 1)
        if can_help
    ]
    text = "\n\n".join(
        [bold(tr().requests_title)]
        + [
            lines(f"{marker(index, total)} {title}", goal)
            for index, (_, title, goal, _) in enumerate(found, 1)
        ]
    )
    return Reply(text, keyboard(rows))


async def help_prompt(
    session: AsyncSession, actor: models.User, now: datetime, request_id: UUID
) -> Reply:
    request = await mentorship_requests.get_help_request(session, actor, request_id, now)
    if HelpRequestAction.OFFER_HELP not in request.allowed_actions:
        return Reply(tr().cant_offer, ok=False)
    return prompt(
        lines(
            tr().help_prompt_head.format(
                topic=bold(request.topic), name=bold(request.learner.display_name)
            ),
            tr().help_prompt_hint,
        )
    )


async def offer_help(
    session: AsyncSession, actor: models.User, now: datetime, request_id: UUID, message: str
) -> Reply:
    body = CreateHelpOfferBody(message=message)
    await help_offers.create_help_offer(session, actor, request_id, body, now)
    return Reply(tr().offer_sent)


async def mentorships(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    live = [
        mentorship
        for mentorship in await mentorship_mentorships.list_my_mentorships(
            session, actor, None, None
        )
        if mentorship.status in LIVE_MENTORSHIP_STATUSES
    ]
    if not live:
        return Reply(tr().no_mentorships)
    areas = await area_names(session)
    texts = tr()
    parts: list[str] = [bold(texts.mentorships_title)]
    rows: list[list[InlineKeyboardButton]] = []
    total = len(live)
    for index, mentorship in enumerate(live, 1):
        request = await session.get(models.HelpRequest, mentorship.request_id)
        topic = request.topic if request else areas.get(mentorship.area_key, mentorship.area_key)
        mentoring = mentorship.mentor_id == actor.id
        other = mentorship.learner if mentoring else mentorship.mentor
        last = (
            texts.last_checkin.format(day(mentorship.last_checkin_at))
            if mentorship.last_checkin_at
            else texts.no_checkins
        )
        state = (
            ""
            if mentorship.status == MentorshipStatus.ACTIVE
            else f"{texts.mentorship_statuses[mentorship.status]} · "
        )
        role = texts.mentoring_with if mentoring else texts.learning_from
        parts.append(
            lines(
                f"{marker(index, total)} {subject(topic, f'/mentorships/{mentorship.id}')} · "
                f"{role.format(esc(other.display_name))}",
                f"  {italic(state + last)}",
            )
        )
        row: list[InlineKeyboardButton] = []
        if MentorshipAction.CHECK_IN in mentorship.allowed_actions:
            row.append(
                button(
                    numbered(index, total, texts.check_in),
                    CheckinCallback(mentorship_id=mentorship.id).pack(),
                )
            )
        if MentorshipAction.CLOSE in mentorship.allowed_actions:
            row.append(
                button(
                    numbered(index, total, texts.close),
                    CloseCallback(mentorship_id=mentorship.id).pack(),
                )
            )
        rows.append(row)
    return Reply("\n".join(parts), keyboard(rows))


async def checkin_prompt(session: AsyncSession, actor: models.User, mentorship_id: UUID) -> Reply:
    mentorship = await mentorship_mentorships.get_mentorship(session, actor, mentorship_id)
    if MentorshipAction.CHECK_IN not in mentorship.allowed_actions:
        return Reply(tr().cant_checkin, ok=False)
    return prompt(tr().checkin_prompt)


async def checkin(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    mentorship_id: UUID,
    note: str,
) -> Reply:
    body = CreateMentorshipCheckinBody(note=note, occurred_on=now.date())
    await mentorship_mentorships.create_checkin(
        session, runtime.jobs, actor, mentorship_id, body, now
    )
    return Reply(tr().checkin_done)


def outcome_keyboard(mentorship_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(label, OutcomeCallback(outcome=outcome, mentorship_id=mentorship_id).pack())
                for outcome, label in tr().outcomes.items()
            ],
            [cancel_button()],
        ]
    )


async def close_prompt(session: AsyncSession, actor: models.User, mentorship_id: UUID) -> Reply:
    mentorship = await mentorship_mentorships.get_mentorship(session, actor, mentorship_id)
    if MentorshipAction.CLOSE not in mentorship.allowed_actions:
        return Reply(tr().cant_close, ok=False)
    return Reply(tr().close_prompt, outcome_keyboard(mentorship_id))


async def close(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    mentorship_id: UUID,
    outcome: MentorshipOutcome,
) -> Reply:
    await mentorship_mentorships.close_mentorship(
        session, runtime.jobs, actor, mentorship_id, CloseMentorshipBody(outcome=outcome), now
    )
    return Reply(tr().mentorship_closed.format(esc(tr().outcomes[outcome].lower())))


async def confirmations(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    groups = await feed_history.list_pending_confirmations(session, actor, now)
    confirmable = {fact_type.value for fact_type in ConfirmationFactType}
    people = await names(session, {fact.user_id for group in groups for fact in group.facts})
    texts = tr()
    parts: list[str] = [bold(texts.confirm_title)]
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        facts = [fact for fact in group.facts if fact.type.value in confirmable]
        if not facts:
            continue
        parts.append(f"• {bold(group.title)}")
        rows.extend(
            [
                button(
                    f"☐ {clip(people.get(fact.user_id, texts.member_fallback), 30)} · "
                    f"{clip(group.title, 24)}",
                    ConfirmCallback(
                        fact_type=ConfirmationFactType(fact.type.value), fact_id=fact.id
                    ).pack(),
                )
            ]
            for fact in facts
        )
    if not rows:
        return Reply(texts.nothing_to_confirm)
    parts.append(texts.confirm_hint)
    return Reply("\n".join(parts), keyboard(rows[:50]))


async def confirm(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
) -> Reply:
    await feed_confirmations.confirm_fact(session, runtime.jobs, actor, fact_type, fact_id, now)
    return Reply(tr().confirmed)


async def profile(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    current = await identity.current_user(session, actor, now)
    metrics = await member_metrics(session, actor.id)
    area_scores = sorted(
        (item for item in await scores.list_scores(session, actor.id, actor) if item.effective > 0),
        key=lambda item: item.effective,
        reverse=True,
    )
    areas = await area_names(session)
    current_teams = [
        subject(project.name, f"/projects/{project.id}")
        for project in await project_records.list_my_projects(session, actor, None)
        if project.team_status in CURRENT_TEAM_STATUSES
    ]
    texts = tr()
    totals = [
        plural(count, noun)
        for count, noun in (
            (metrics.hackathons_finished, texts.hackathon),
            (metrics.projects_shipped, texts.shipped_project),
            (metrics.mentorships_completed, texts.completed_mentorship),
        )
        if count
    ]
    level = [
        part
        for part in (
            texts.xp.format(f"{metrics.xp:.0f}") if metrics.xp else None,
            texts.activity.format(f"{metrics.activity:.1f}") if metrics.activity else None,
        )
        if part
    ]
    top = ", ".join(
        f"{esc(areas.get(item.area_key, item.area_key))} {item.effective:.1f}"
        for item in area_scores[:6]
    )
    dnd_until = current.dnd_until
    dnd_on = dnd_until is not None and (isinstance(dnd_until, str) or dnd_until > now)
    body = lines(
        f"👤 {subject(current.display_name, f'/users/{actor.id}')} · @{esc(current.github_login)}",
        " · ".join(level) or None,
        " · ".join(totals) or None,
        texts.top_areas.format(top) if top else None,
        texts.teams_line.format(", ".join(current_teams)) if current_teams else None,
        dnd_line(dnd_until, now) if dnd_on else None,
    )
    if not (level or totals or top or current_teams):
        body = lines(body, texts.profile_empty)
    return Reply(body)


def dnd_line(value: datetime | str | None, now: datetime) -> str:
    texts = tr()
    if value is None:
        return texts.dnd_off
    if isinstance(value, str):
        return texts.dnd_forever
    if value <= now:
        return texts.dnd_off
    return texts.dnd_until.format(when(value))


async def dnd(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    argument: str | None,
) -> Reply:
    if argument is None:
        return Reply(
            lines(
                dnd_line(profiles.availability_view(actor).dnd_until, now),
                esc(tr().dnd_help),
            )
        )
    if argument.lower() == "off":
        user = await profiles.set_availability(session, actor, None)
    elif argument.isdigit() and 1 <= int(argument) <= DND_MAX_DAYS:
        user = await profiles.set_availability(session, actor, now + timedelta(days=int(argument)))
    else:
        return usage("dnd")
    return Reply(dnd_line(profiles.availability_view(user).dnd_until, now))


async def action(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    key: ActionKey,
    entity_id: UUID,
) -> Reply:
    await run_action(session, runtime.jobs, actor, key, entity_id, now)
    return Reply(tr().action_done[key])


async def menu(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime, item: MenuItem
) -> Reply:
    match item:
        case MenuItem.EVENTS:
            return await events(runtime, session, actor, now)
        case MenuItem.TEAM:
            return await teams(runtime, session, actor, now)
        case MenuItem.MENTORSHIPS:
            return await mentorships(runtime, session, actor, now)
        case MenuItem.PROFILE:
            return await profile(runtime, session, actor, now)
