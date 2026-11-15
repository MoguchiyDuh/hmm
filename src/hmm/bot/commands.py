from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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
    MenuCallback,
    MenuItem,
    OutcomeCallback,
)
from hmm.bot.fmt import (
    View,
    bold,
    clip,
    day,
    esc,
    headcount,
    humanize,
    italic,
    lines,
    plural,
    quote,
    span,
    subject,
    viewing,
    web_url,
    when,
)
from hmm.bot.runtime import LINK_PATH, BotRuntime, Reply
from hmm.catalog import views as catalog_views
from hmm.common import (
    AttendanceStatus,
    ConfirmationFactType,
    EnrollmentStatus,
    ErrorCode,
    EventFormat,
    EventPhase,
    FormationPhase,
    HelpRequestFormat,
    HelpRequestStatus,
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
from hmm.projects import invites as project_invites
from hmm.projects import records as project_records
from hmm.scores import service as scores
from hmm.scores.snapshot import member_metrics


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    args: str
    text: str
    listed: bool = True


COMMANDS: tuple[Command, ...] = (
    Command("events", "", "hackathons with open registration"),
    Command("team", "", "your teams and pending placements"),
    Command("invites", "", "pending invites"),
    Command("profile", "", "your profile"),
    Command("ask", "", "ask for help"),
    Command("requests", "", "help requests you could take"),
    Command("mentorships", "", "your mentorships"),
    Command("confirm", "", "confirm people you worked with"),
    Command("dnd", " [off|<days>]", "do not disturb"),
    Command("settings", "", "notification settings"),
    Command("unlink", "", "disconnect this Telegram account"),
    Command("help", "", "this list"),
    Command("event", " <slug>", "event card", listed=False),
    Command("enroll", " <slug>", "join the team formation queue", listed=False),
)

MENU_COMMANDS = tuple(command for command in COMMANDS if command.listed)

HELP_TEXT = lines(
    bold("hmm bot"),
    *(f"/{command.name}{esc(command.args)} — {esc(command.text)}" for command in MENU_COMMANDS),
)

UNKNOWN_TEXT = "Didn't get that. Send /help to see what I can do."

ACTION_DONE: dict[ActionKey, str] = {
    ActionKey.INVITE_ACCEPT: "✅ Invite accepted.",
    ActionKey.INVITE_DECLINE: "Invite declined.",
    ActionKey.JOIN_REQUEST_ACCEPT: "✅ Join request accepted.",
    ActionKey.JOIN_REQUEST_REJECT: "Join request rejected.",
    ActionKey.PLACEMENT_ACCEPT: "✅ You're on the team.",
    ActionKey.PLACEMENT_DECLINE: "Placement declined.",
    ActionKey.EVENT_ENROLL: "✅ Enrolled again.",
    ActionKey.HELP_OFFER_ACCEPT: "✅ Offer accepted. Your mentorship has started.",
    ActionKey.HELP_REQUEST_DECLINE: "Help request declined.",
    ActionKey.HELP_REQUEST_OPEN: "✅ Help request opened to everyone.",
    ActionKey.MENTORSHIP_ACKNOWLEDGE: "✅ Outcome acknowledged.",
}

OUTCOME_LABELS: dict[MentorshipOutcome, str] = {
    MentorshipOutcome.REACHED: "Goal reached",
    MentorshipOutcome.PARTIAL: "Partly",
    MentorshipOutcome.NOT_REACHED: "Not reached",
}

ENROLLMENT_LABELS: dict[EnrollmentStatus, str] = {
    EnrollmentStatus.ACTIVE: "✅ You're in the queue.",
    EnrollmentStatus.PLACED: "✅ You're placed on a team.",
    EnrollmentStatus.RESERVE: "You're in the reserve.",
}

FORMAT_LABELS: dict[EventFormat, str] = {
    EventFormat.ONLINE: "online",
    EventFormat.OFFLINE: "in person",
    EventFormat.HYBRID: "hybrid",
}

MENU_LABELS: dict[MenuItem, str] = {
    MenuItem.EVENTS: "📅 Events",
    MenuItem.TEAM: "👥 My teams",
    MenuItem.MENTORSHIPS: "🤝 Mentorships",
    MenuItem.PROFILE: "👤 Profile",
}

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


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("Cancel", CancelCallback().pack())]])


def prompt(text: str) -> Reply:
    return Reply(text, cancel_markup())


def menu_markup() -> InlineKeyboardMarkup:
    items = list(MENU_LABELS.items())
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
    for item in COMMANDS:
        if item.name == command:
            return Reply(f"Usage: /{item.name}{esc(item.args)} — {esc(item.text)}")
    raise KeyError(command)


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
    session: AsyncSession,
    now: datetime,
    *,
    code: str,
    telegram_user_id: int,
    chat_id: int,
    username: str | None,
) -> Reply:
    actor = await identity.link_telegram(session, code, telegram_user_id, chat_id, username, now)
    return Reply(
        lines(
            f"✅ Linked to {bold(actor.display_name)}.",
            "Notifications will arrive here. Where to start:",
        ),
        menu_markup(),
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
            "👋 This is the hmm bot.",
            f"To link your account, open {esc(runtime.url(LINK_PATH))} and tap “Link Telegram”.",
        )
    )


async def greet(runtime: BotRuntime, session: AsyncSession, telegram_user_id: int) -> Reply:
    name = await linked_name(session, telegram_user_id)
    if name is None:
        return link_instructions(runtime)
    return Reply(f"👋 Hi, {bold(name)}. What do you need?", menu_markup())


async def unknown(runtime: BotRuntime, session: AsyncSession, telegram_user_id: int) -> Reply:
    if await linked_name(session, telegram_user_id) is None:
        return link_instructions(runtime)
    return Reply(UNKNOWN_TEXT)


async def unlink(session: AsyncSession, telegram_user_id: int) -> Reply:
    await identity.unlink_telegram_account(session, telegram_user_id)
    return Reply("Unlinked. You won't get notifications here anymore.")


def settings_reply(runtime: BotRuntime) -> Reply:
    with viewing(View(public_url=runtime.config.public_url)):
        return with_web(
            "Choose which notifications reach Telegram in your hmm settings.",
            [],
            "/settings#notifications",
            "Notification settings",
        )


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
        return Reply("No events have open registration right now.")
    parts: list[str] = [bold("Open registration")]
    for event in page.items:
        parts.append(
            lines(
                f"• {subject(event.title, f'/events/{event.id}')} · {esc(humanize(event.kind))}",
                f"  {span(event.starts_at, event.ends_at, event.timezone)}",
                f"  Register by {when(event.registration_deadline, event.timezone)}"
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
    match formation.phase:
        case FormationPhase.OPEN:
            timing = f"Teams form {when(formation.formation_at, timezone)}."
        case FormationPhase.FORMING:
            timing = "Teams are being formed now."
        case _:
            timing = "Teams are formed."
    status = formation.my_enrollment.status if formation.my_enrollment else None
    return [
        timing,
        f"{headcount(queued)} in the queue, about {plural(projected, 'team')} so far."
        if queued and formation.phase == FormationPhase.OPEN
        else None,
        ENROLLMENT_LABELS.get(status) if status else None,
        f"Your team: {subject(formation.my_team.name, f'/projects/{formation.my_team.id}')}"
        if formation.my_team
        else None,
    ]


async def event_card(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    event_id: UUID,
    notice: str | None = None,
) -> Reply:
    event = await catalog_views.visible_event(session, event_id, actor)
    detail = await catalog_views.event_detail(session, event, actor, now)
    tz = detail.timezone
    facts = [
        FORMAT_LABELS[detail.format] if detail.format else None,
        f"teams of {detail.team_size_min} to {detail.team_size_max}"
        if detail.team_size_min and detail.team_size_max
        else None,
        f"{detail.attendance_counts.going} going" if detail.attendance_counts.going else None,
    ]
    going = (
        detail.my_attendance is not None and detail.my_attendance.status == AttendanceStatus.GOING
    )
    text = lines(
        f"{notice}\n" if notice else None,
        f"{subject(detail.title, f'/events/{detail.id}')} · {esc(humanize(detail.kind))}",
        span(detail.starts_at, detail.ends_at, tz),
        f"Register by {when(detail.registration_deadline, tz)}"
        if detail.registration_deadline
        else None,
        esc(" · ".join(fact for fact in facts if fact)) or None,
        quote(detail.summary, 300),
        "✅ You're going." if going else None,
        *formation_lines(detail.formation, tz),
    )
    ops = [
        (EventAction.GOING, EventOp.GOING, "I'm going"),
        (EventAction.ENROLL, EventOp.ENROLL, "Find me a team"),
        (EventAction.CREATE_PARTY, EventOp.PARTY, "Start a party"),
        (EventAction.WITHDRAW, EventOp.WITHDRAW, "Leave the queue"),
    ]
    actions = [
        button(label, EventCallback(op=op, event_id=detail.id).pack())
        for action, op, label in ops
        if action in detail.allowed_actions and not (op == EventOp.GOING and going)
    ]
    rows = [actions[start : start + 2] for start in range(0, len(actions), 2)]
    back = EventCallback(op=EventOp.LIST, event_id=detail.id).pack()
    rows.append([button("⬅️ All events", back)])
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
            notice = "✅ Marked as going."
        case EventOp.ENROLL:
            await formation_queue.enroll(session, event_id, CreateEnrollmentBody(), actor, now)
            notice = "✅ You're in the queue. I'll message you when teams form."
        case EventOp.WITHDRAW:
            await formation_queue.withdraw_enrollment(session, event_id, actor, now)
            notice = "You left the queue."
        case EventOp.PARTY:
            await formation_parties.create_party(session, event_id, actor, now)
            notice = "✅ Party created. Invite teammates on the web."
    card = await event_card(runtime, session, actor, now, event_id, notice)
    if op != EventOp.PARTY:
        return card
    url = web_url(f"/events/{event_id}")
    if url is None or card.markup is None:
        return card
    rows = [[InlineKeyboardButton(text="Invite teammates", url=url)], *card.markup.inline_keyboard]
    return Reply(card.text, InlineKeyboardMarkup(inline_keyboard=rows))


async def enroll(session: AsyncSession, actor: models.User, now: datetime, event_id: UUID) -> Reply:
    await formation_queue.enroll(session, event_id, CreateEnrollmentBody(), actor, now)
    event = await session.get(models.Event, event_id)
    formation_at = await session.scalar(
        select(models.EventFormation.formation_at).where(models.EventFormation.event_id == event_id)
    )
    title = subject(event.title, f"/events/{event_id}") if event else "the event"
    timezone = event.timezone if event else None
    return Reply(
        lines(
            f"✅ You're in the queue for {title}.",
            f"Teams form {when(formation_at, timezone)}." if formation_at else None,
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
        return Reply("You're not on any hackathon team right now. See /events.")
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
    parts: list[str] = [bold("Your teams")]
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
                f"  ⏳ Accept your place by {when(accept_by, event.timezone if event else None)}"
                if accept_by
                else None,
            )
        )
        if accept_by:
            rows.append(
                [
                    button(
                        numbered(index, total, "Accept"),
                        ActionCallback(key=ActionKey.PLACEMENT_ACCEPT, entity_id=project.id).pack(),
                    ),
                    button(
                        numbered(index, total, "Decline"),
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
        return Reply("No pending invites.")
    inviters = await names(session, {row.invited_by for row in pending if row.invited_by})
    parts: list[str] = [bold("Pending invites")]
    rows: list[list[InlineKeyboardButton]] = []
    total = len(pending)
    for index, invite in enumerate(pending, 1):
        if invite.project_id is not None:
            project = await session.get(models.Project, invite.project_id)
            target = (
                f"team {subject(project.name, f'/projects/{project.id}')}" if project else "a team"
            )
        else:
            event = await session.get(models.Event, invite.event_id) if invite.event_id else None
            target = (
                f"a party for {subject(event.title, f'/events/{event.id}')}" if event else "a party"
            )
        inviter = inviters.get(invite.invited_by) if invite.invited_by else None
        parts.append(
            lines(
                f"{marker(index, total)} {bold(inviter) if inviter else 'Someone'} "
                f"invited you to {target}.",
                f"Expires {when(invite.expires_at)}.",
            )
        )
        rows.append(
            [
                button(
                    numbered(index, total, "Accept"),
                    ActionCallback(key=ActionKey.INVITE_ACCEPT, entity_id=invite.id).pack(),
                ),
                button(
                    numbered(index, total, "Decline"),
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
    grid.append([button("Cancel", CancelCallback().pack())])
    return Reply("What do you need help with? Pick an area.", keyboard(grid))


async def ask_topic(session: AsyncSession, area_key: str) -> tuple[Reply, dict[str, str]]:
    area = await mentor_offers.load_area(session, area_key)
    return (
        prompt(
            lines(
                f"Help with {bold(area.name)}.",
                "What's the topic? A few words, like “Async Python” or “System design”.",
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
                f"Asking for help with {bold(topic)} in {bold(area.name)}.",
                "What do you want to achieve? Send it as one message.",
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
            f"✅ Help request {subject(request.topic, f'/help-requests/{request.id}')} created.",
            "Matching mentors will be notified, and offers will arrive here.",
        )
    )


async def requests(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    offers = await mentor_offers.mentor_offers_view(session, actor)
    area_keys = [offer.area_key for offer in offers.offers if offer.active]
    if not area_keys:
        return with_web(
            "You don't offer mentoring yet. Pick your areas on the web to see requests here.",
            [],
            "/mentorship",
            "Set up mentoring",
        )
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
        return Reply("No open help requests in your areas right now.")
    total = len(found)
    rows = [
        [button(numbered(index, total, "Help"), HelpCallback(request_id=request_id).pack())]
        for index, (request_id, _, _, can_help) in enumerate(found, 1)
        if can_help
    ]
    text = "\n\n".join(
        [bold("Open help requests in your areas")]
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
        raise ProblemError(ErrorCode.INVALID_STATE, "You can't offer help on this request")
    return prompt(
        lines(
            f"Offering help with {bold(request.topic)} to {bold(request.learner.display_name)}.",
            "Send a short message for the learner.",
        )
    )


async def offer_help(
    session: AsyncSession, actor: models.User, now: datetime, request_id: UUID, message: str
) -> Reply:
    body = CreateHelpOfferBody(message=message)
    await help_offers.create_help_offer(session, actor, request_id, body, now)
    return Reply("✅ Offer sent. You'll hear back here if the learner accepts.")


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
        return Reply("No active mentorships. Ask for help with /ask or offer it with /requests.")
    areas = await area_names(session)
    parts: list[str] = [bold("Your mentorships")]
    rows: list[list[InlineKeyboardButton]] = []
    total = len(live)
    for index, mentorship in enumerate(live, 1):
        request = await session.get(models.HelpRequest, mentorship.request_id)
        topic = request.topic if request else areas.get(mentorship.area_key, mentorship.area_key)
        mentoring = mentorship.mentor_id == actor.id
        other = mentorship.learner if mentoring else mentorship.mentor
        last = (
            f"last check-in {day(mentorship.last_checkin_at)}"
            if mentorship.last_checkin_at
            else "no check-ins yet"
        )
        state = "" if mentorship.status == MentorshipStatus.ACTIVE else f"{mentorship.status} · "
        parts.append(
            lines(
                f"{marker(index, total)} {subject(topic, f'/mentorships/{mentorship.id}')} · "
                f"{'mentoring' if mentoring else 'learning from'} {esc(other.display_name)}",
                f"  {italic(state + last)}",
            )
        )
        row: list[InlineKeyboardButton] = []
        if MentorshipAction.CHECK_IN in mentorship.allowed_actions:
            row.append(
                button(
                    numbered(index, total, "Check in"),
                    CheckinCallback(mentorship_id=mentorship.id).pack(),
                )
            )
        if MentorshipAction.CLOSE in mentorship.allowed_actions:
            row.append(
                button(
                    numbered(index, total, "Close"),
                    CloseCallback(mentorship_id=mentorship.id).pack(),
                )
            )
        rows.append(row)
    return Reply("\n".join(parts), keyboard(rows))


async def checkin_prompt(session: AsyncSession, actor: models.User, mentorship_id: UUID) -> Reply:
    mentorship = await mentorship_mentorships.get_mentorship(session, actor, mentorship_id)
    if MentorshipAction.CHECK_IN not in mentorship.allowed_actions:
        raise ProblemError(ErrorCode.INVALID_STATE, "You can't check in on this mentorship")
    return prompt("What did you work on? Send a short note.")


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
    return Reply("✅ Check-in recorded.")


def outcome_keyboard(mentorship_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(label, OutcomeCallback(outcome=outcome, mentorship_id=mentorship_id).pack())
                for outcome, label in OUTCOME_LABELS.items()
            ],
            [button("Cancel", CancelCallback().pack())],
        ]
    )


async def close_prompt(session: AsyncSession, actor: models.User, mentorship_id: UUID) -> Reply:
    mentorship = await mentorship_mentorships.get_mentorship(session, actor, mentorship_id)
    if MentorshipAction.CLOSE not in mentorship.allowed_actions:
        raise ProblemError(ErrorCode.INVALID_STATE, "You can't close this mentorship")
    return Reply("Did you reach the goal?", outcome_keyboard(mentorship_id))


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
    return Reply(f"✅ Mentorship closed: {esc(OUTCOME_LABELS[outcome].lower())}.")


async def confirmations(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    groups = await feed_history.list_pending_confirmations(session, actor, now)
    confirmable = {fact_type.value for fact_type in ConfirmationFactType}
    people = await names(session, {fact.user_id for group in groups for fact in group.facts})
    parts: list[str] = [bold("Confirm who you worked with")]
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        facts = [fact for fact in group.facts if fact.type.value in confirmable]
        if not facts:
            continue
        parts.append(f"• {bold(group.title)}")
        rows.extend(
            [
                button(
                    f"☐ {clip(people.get(fact.user_id, 'Member'), 30)} · {clip(group.title, 24)}",
                    ConfirmCallback(
                        fact_type=ConfirmationFactType(fact.type.value), fact_id=fact.id
                    ).pack(),
                )
            ]
            for fact in facts
        )
    if not rows:
        return Reply("Nothing to confirm right now.")
    parts.append("Tap everyone you actually worked with.")
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
    return Reply("✅ Confirmed.")


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
    totals = [
        plural(count, noun)
        for count, noun in (
            (metrics.hackathons_finished, "hackathon"),
            (metrics.projects_shipped, "shipped project"),
            (metrics.mentorships_completed, "completed mentorship"),
        )
        if count
    ]
    level = [
        part
        for part in (
            f"XP {metrics.xp:.0f}" if metrics.xp else None,
            f"activity {metrics.activity:.1f}" if metrics.activity else None,
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
        f"Top areas: {top}" if top else None,
        f"Teams: {', '.join(current_teams)}" if current_teams else None,
        dnd_line(dnd_until, now) if dnd_on else None,
    )
    if not (level or totals or top or current_teams):
        body = lines(body, "Nothing here yet. Join an event with /events.")
    return Reply(body)


def dnd_line(value: datetime | str | None, now: datetime) -> str:
    if value is None:
        return "🔔 Do not disturb is off."
    if isinstance(value, str):
        return "🔕 Do not disturb is on until you turn it off."
    if value <= now:
        return "🔔 Do not disturb is off."
    return f"🔕 Do not disturb is on until {when(value)}."


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
                esc("/dnd <days> turns it on, /dnd off turns it off."),
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
    return Reply(ACTION_DONE[key])


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
