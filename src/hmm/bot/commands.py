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
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    EventCallback,
    EventOp,
    HelpCallback,
    OutcomeCallback,
)
from hmm.bot.fmt import bold, clip, day, esc, humanize, italic, lines, link, plural, quote, when
from hmm.bot.runtime import LINK_PATH, BotRuntime, Reply
from hmm.catalog import views as catalog_views
from hmm.common import (
    AttendanceStatus,
    ConfirmationFactType,
    ErrorCode,
    EventPhase,
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
from hmm.formation.schemas import CreateEnrollmentBody
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

COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("events", "", "hackathons with open registration"),
    ("event", " <id>", "event card"),
    ("enroll", " <id>", "join the team formation queue"),
    ("team", "", "your teams and pending placements"),
    ("invites", "", "pending invites"),
    ("me", "", "your profile"),
    ("ask", " <area> <topic>", "ask for help"),
    ("requests", "", "help requests you could take"),
    ("mentorships", "", "your mentorships"),
    ("confirm", "", "confirm people you worked with"),
    ("dnd", " [off|<days>]", "do not disturb"),
    ("settings", "", "notification settings"),
    ("unlink", "", "disconnect this Telegram account"),
    ("cancel", "", "stop the current question"),
    ("help", "", "this list"),
)

HELP_TEXT = lines(
    bold("hmm bot"),
    *(f"/{name}{esc(args)} — {esc(text)}" for name, args, text in COMMANDS),
)

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

CURRENT_TEAM_STATUSES = frozenset({TeamStatus.FORMED, TeamStatus.ACTIVE})
LIVE_MENTORSHIP_STATUSES = frozenset(
    {MentorshipStatus.ACTIVE, MentorshipStatus.STALLED, MentorshipStatus.CLOSING}
)
LIST_LIMIT = 10
DND_MAX_DAYS = 365


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def with_web(
    runtime: BotRuntime,
    text: str,
    rows: list[list[InlineKeyboardButton]],
    path: str,
    label: str = "Open in hmm",
) -> Reply:
    url = runtime.url(path)
    if url.startswith("https://"):
        rows = [*rows, [InlineKeyboardButton(text=label, url=url)]]
    else:
        text = f"{text}\n\n{link(url, label)}"
    return Reply(text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


def usage(command: str) -> Reply:
    for name, args, text in COMMANDS:
        if name == command:
            return Reply(f"Usage: /{name}{esc(args)} — {esc(text)}")
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
            "Notifications will arrive here. Send /help for commands.",
        )
    )


async def greet(runtime: BotRuntime, session: AsyncSession, telegram_user_id: int) -> Reply:
    linked = await session.scalar(
        select(models.User.display_name)
        .join(models.TelegramLink, models.TelegramLink.user_id == models.User.id)
        .where(models.TelegramLink.telegram_user_id == telegram_user_id)
    )
    if linked is None:
        return Reply(
            lines(
                "👋 This is the hmm bot.",
                f"To link your account, open {link(runtime.url(LINK_PATH), 'hmm settings')} "
                "and tap “Link Telegram”.",
            )
        )
    return Reply(lines(f"👋 Hi, {bold(linked)}.", "", HELP_TEXT))


async def unlink(session: AsyncSession, telegram_user_id: int) -> Reply:
    await identity.unlink_telegram_account(session, telegram_user_id)
    return Reply("Unlinked. You won't get notifications here anymore.")


def settings_reply(runtime: BotRuntime) -> Reply:
    return with_web(
        runtime,
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
    rows: list[list[InlineKeyboardButton]] = []
    for index, event in enumerate(page.items, 1):
        deadline = (
            f", register by {when(event.registration_deadline, event.timezone)}"
            if event.registration_deadline
            else ""
        )
        parts.append(
            lines(
                f"{index}. {bold(event.title)} · {esc(humanize(event.kind))}",
                f"{day(event.starts_at, event.timezone)} to "
                f"{day(event.ends_at, event.timezone)}{deadline}",
            )
        )
        row = [
            button(f"{index} · {label}", EventCallback(op=op, event_id=event.id).pack())
            for action, op, label in (
                (None, EventOp.SHOW, "Details"),
                (EventAction.GOING, EventOp.GOING, "Going"),
                (EventAction.ENROLL, EventOp.ENROLL, "Enroll"),
            )
            if action is None or action in event.allowed_actions
        ]
        rows.append(row)
    return with_web(runtime, "\n\n".join(parts), rows, "/events", "All events")


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
    team_size = (
        f"Teams of {detail.team_size_min} to {detail.team_size_max}"
        if detail.team_size_min and detail.team_size_max
        else None
    )
    parts: list[str | None] = [
        f"{bold(detail.title)} · {esc(humanize(detail.kind))}",
        f"{when(detail.starts_at, tz)} to {when(detail.ends_at, tz)}",
        f"Register by {when(detail.registration_deadline, tz)}"
        if detail.registration_deadline
        else None,
        esc(humanize(detail.format)) if detail.format else None,
        team_size,
        quote(detail.summary, 300),
        f"Going: {detail.attendance_counts.going}",
    ]
    formation = detail.formation
    if formation is not None:
        projection = formation.projection
        parts.extend(
            [
                f"Formation: {bold(humanize(formation.phase))}, "
                f"teams form {when(formation.formation_at, tz)}",
                f"Live projection: {plural(len(projection.new_teams), 'new team')}, "
                f"{plural(len(projection.fills), 'refilled team')}, "
                f"{plural(len(projection.queue), 'unit')} in the queue",
                f"Your enrollment: {bold(humanize(formation.my_enrollment.status))}"
                if formation.my_enrollment
                else None,
                f"Your team: {bold(formation.my_team.name)}" if formation.my_team else None,
            ]
        )
    ops = [
        (EventAction.GOING, EventOp.GOING, "Going"),
        (EventAction.ENROLL, EventOp.ENROLL, "Enroll"),
        (EventAction.WITHDRAW, EventOp.WITHDRAW, "Withdraw"),
        (EventAction.CREATE_PARTY, EventOp.PARTY, "Start a party"),
    ]
    row = [
        button(label, EventCallback(op=op, event_id=detail.id).pack())
        for action, op, label in ops
        if action in detail.allowed_actions
    ]
    return with_web(runtime, lines(*parts), [row] if row else [], f"/events/{detail.id}")


async def event_op(
    runtime: BotRuntime,
    session: AsyncSession,
    actor: models.User,
    now: datetime,
    op: EventOp,
    event_id: UUID,
) -> Reply:
    if op == EventOp.SHOW:
        return await event_card(runtime, session, actor, now, event_id)
    event = await catalog_views.visible_event(session, event_id, actor)
    title = bold(event.title)
    match op:
        case EventOp.GOING:
            await attendance_events.set_attendance(
                session,
                runtime.jobs,
                event_id,
                SetAttendanceBody(status=AttendanceStatus.GOING),
                actor,
                now,
            )
            return Reply(f"✅ You're going to {title}.")
        case EventOp.ENROLL:
            return await enroll(session, actor, now, event_id)
        case EventOp.WITHDRAW:
            await formation_queue.withdraw_enrollment(session, event_id, actor, now)
            return Reply(f"You left the formation queue for {title}.")
        case EventOp.PARTY:
            await formation_parties.create_party(session, event_id, actor, now)
            return with_web(
                runtime,
                f"✅ Party created for {title}. Invite teammates on the web.",
                [],
                f"/events/{event_id}",
            )
    raise AssertionError(op)


async def enroll(session: AsyncSession, actor: models.User, now: datetime, event_id: UUID) -> Reply:
    await formation_queue.enroll(session, event_id, CreateEnrollmentBody(), actor, now)
    event = await session.get(models.Event, event_id)
    formation_at = await session.scalar(
        select(models.EventFormation.formation_at).where(models.EventFormation.event_id == event_id)
    )
    title = bold(event.title) if event else "the event"
    timezone = event.timezone if event else None
    return Reply(
        lines(
            f"✅ Enrolled in {title}.",
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
    for index, project in enumerate(projects, 1):
        event = events_by_id.get(project.event_id) if project.event_id else None
        member = memberships.get(project.id)
        pending = (
            member is not None
            and member.state == MemberState.PROPOSED
            and member.accept_by is not None
            and member.accept_by > now
        )
        parts.append(
            lines(
                f"{index}. {bold(project.name)}"
                + (f" · {esc(event.title)}" if event else "")
                + f" · {esc(humanize(project.team_status))}",
                f"⏳ Placement waiting for you until "
                f"{when(member.accept_by, event.timezone if event else None)}"
                if pending and member is not None and member.accept_by is not None
                else None,
            )
        )
        if pending:
            rows.append(
                [
                    button(
                        f"{index} · Accept",
                        ActionCallback(key=ActionKey.PLACEMENT_ACCEPT, entity_id=project.id).pack(),
                    ),
                    button(
                        f"{index} · Decline",
                        ActionCallback(
                            key=ActionKey.PLACEMENT_DECLINE, entity_id=project.id
                        ).pack(),
                    ),
                ]
            )
    return with_web(runtime, "\n\n".join(parts), rows, "/projects")


async def invites(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    pending = await project_invites.my_invites(session, actor, "pending")
    if not pending:
        return Reply("No pending invites.")
    inviters = await names(session, {row.invited_by for row in pending if row.invited_by})
    parts: list[str] = [bold("Pending invites")]
    rows: list[list[InlineKeyboardButton]] = []
    for index, invite in enumerate(pending, 1):
        if invite.project_id is not None:
            project = await session.get(models.Project, invite.project_id)
            target = f"team {bold(project.name)}" if project else "a team"
        else:
            event = await session.get(models.Event, invite.event_id) if invite.event_id else None
            target = f"a party for {bold(event.title)}" if event else "a party"
        inviter = inviters.get(invite.invited_by) if invite.invited_by else None
        parts.append(
            lines(
                f"{index}. {bold(inviter) if inviter else 'Someone'} invited you to {target}.",
                f"Expires {when(invite.expires_at)}.",
            )
        )
        rows.append(
            [
                button(
                    f"{index} · Accept",
                    ActionCallback(key=ActionKey.INVITE_ACCEPT, entity_id=invite.id).pack(),
                ),
                button(
                    f"{index} · Decline",
                    ActionCallback(key=ActionKey.INVITE_DECLINE, entity_id=invite.id).pack(),
                ),
            ]
        )
    return Reply("\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows))


async def start_ask(
    session: AsyncSession, area_key: str, topic: str
) -> tuple[Reply, dict[str, str]]:
    area = await mentor_offers.load_area(session, area_key)
    CreateHelpRequestBody(area_key=area.key, topic=topic, goal="-", format=HelpRequestFormat.ANY)
    return (
        Reply(
            lines(
                f"Asking for help with {bold(topic)} in {bold(area.name)}.",
                "What do you want to achieve? Send it as one message, or /cancel.",
            )
        ),
        {"area_key": area.key, "topic": topic},
    )


async def ask_areas(session: AsyncSession) -> Reply:
    rows = await session.execute(
        select(models.Area.key, models.Area.name)
        .where(models.Area.archived_at.is_(None))
        .order_by(models.Area.sort, models.Area.key)
    )
    areas = ", ".join(f"<code>{esc(key)}</code> ({esc(name)})" for key, name in rows)
    return Reply(lines(usage("ask").text, f"Areas: {areas}" if areas else None))


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
    return with_web(
        runtime,
        lines(
            f"✅ Help request {bold(request.topic)} created.",
            "Matching mentors will be notified, and offers will arrive here.",
        ),
        [],
        f"/help-requests/{request.id}",
    )


async def requests(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    offers = await mentor_offers.mentor_offers_view(session, actor)
    area_keys = [offer.area_key for offer in offers.offers if offer.active]
    if not area_keys:
        return with_web(
            runtime,
            "You don't offer mentoring in any area yet. Set it up on the web.",
            [],
            "/mentorship",
        )
    areas = await area_names(session)
    seen: set[UUID] = set()
    parts: list[str] = [bold("Open help requests in your areas")]
    rows: list[list[InlineKeyboardButton]] = []
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
            if request.id in seen or len(seen) >= LIST_LIMIT:
                continue
            seen.add(request.id)
            index = len(seen)
            area = areas.get(request.area_key, request.area_key)
            parts.append(
                lines(
                    f"{index}. {bold(request.topic)} · {esc(area)} · "
                    f"{esc(request.learner.display_name)}",
                    quote(request.goal, 200),
                )
            )
            if HelpRequestAction.OFFER_HELP in request.allowed_actions:
                rows.append([button(f"{index} · Help", HelpCallback(request_id=request.id).pack())])
    if not seen:
        return Reply("No open help requests in your areas right now.")
    return Reply("\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


async def help_prompt(
    session: AsyncSession, actor: models.User, now: datetime, request_id: UUID
) -> Reply:
    request = await mentorship_requests.get_help_request(session, actor, request_id, now)
    if HelpRequestAction.OFFER_HELP not in request.allowed_actions:
        raise ProblemError(ErrorCode.INVALID_STATE, "You can't offer help on this request")
    return Reply(
        lines(
            f"Offering help with {bold(request.topic)} to {bold(request.learner.display_name)}.",
            "Send a short message for the learner, or /cancel.",
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
        return Reply("No active mentorships. See /requests or /ask.")
    areas = await area_names(session)
    parts: list[str] = [bold("Your mentorships")]
    rows: list[list[InlineKeyboardButton]] = []
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
        parts.append(
            lines(
                f"{index}. {bold(topic)} · {'mentoring' if mentoring else 'learning from'} "
                f"{esc(other.display_name)}",
                italic(f"{humanize(mentorship.status)} · {last}"),
            )
        )
        row: list[InlineKeyboardButton] = []
        if MentorshipAction.CHECK_IN in mentorship.allowed_actions:
            row.append(
                button(f"{index} · Check in", CheckinCallback(mentorship_id=mentorship.id).pack())
            )
        if MentorshipAction.CLOSE in mentorship.allowed_actions:
            row.append(
                button(f"{index} · Close", CloseCallback(mentorship_id=mentorship.id).pack())
            )
        if row:
            rows.append(row)
    return Reply("\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


async def checkin_prompt(session: AsyncSession, actor: models.User, mentorship_id: UUID) -> Reply:
    mentorship = await mentorship_mentorships.get_mentorship(session, actor, mentorship_id)
    if MentorshipAction.CHECK_IN not in mentorship.allowed_actions:
        raise ProblemError(ErrorCode.INVALID_STATE, "You can't check in on this mentorship")
    return Reply("What did you work on? Send a short note, or /cancel.")


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
            ]
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
        parts.append(f"{bold(group.title)} · {esc(humanize(group.subject_type))}")
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
    return Reply("\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows[:50]))


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


async def me(
    runtime: BotRuntime, session: AsyncSession, actor: models.User, now: datetime
) -> Reply:
    current = await identity.current_user(session, actor, now)
    metrics = await member_metrics(session, actor.id)
    area_scores = sorted(
        await scores.list_scores(session, actor.id, actor),
        key=lambda item: item.effective,
        reverse=True,
    )
    areas = await area_names(session)
    current_teams = [
        project.name
        for project in await project_records.list_my_projects(session, actor, None)
        if project.team_status in CURRENT_TEAM_STATUSES
    ]
    top = ", ".join(
        f"{esc(areas.get(item.area_key, item.area_key))} {item.effective:.1f}"
        for item in area_scores[:6]
    )
    return with_web(
        runtime,
        lines(
            f"👤 {bold(current.display_name)} · @{esc(current.github_login)}",
            f"XP {metrics.xp:.0f} · activity {metrics.activity:.1f}",
            f"{plural(metrics.hackathons_finished, 'hackathon')} finished · "
            f"{plural(metrics.projects_shipped, 'project')} shipped · "
            f"{plural(metrics.mentorships_completed, 'mentorship')} completed",
            f"Scores: {top}" if top else None,
            f"Teams: {', '.join(esc(name) for name in current_teams)}" if current_teams else None,
            dnd_line(current.dnd_until, now),
        ),
        [],
        f"/users/{actor.id}",
        "Profile",
    )


def dnd_line(value: datetime | str | None, now: datetime) -> str:
    if value is None:
        return "Do not disturb: off"
    if isinstance(value, str):
        return "Do not disturb: on until you turn it off"
    if value <= now:
        return "Do not disturb: off"
    return f"Do not disturb: on until {when(value)}"


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
                esc("Use /dnd <days> to turn it on, /dnd off to turn it off."),
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
