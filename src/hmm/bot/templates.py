from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hmm.bot.callbacks import (
    ActionCallback,
    CheckinCallback,
    CloseCallback,
    ConfirmCallback,
    HelpCallback,
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
    link,
    moment_or_text,
    people,
    person,
    plural,
    quote,
    reason,
    score,
    span,
    subject,
    viewing,
    when,
)
from hmm.common import AlertSeverity, ConfirmationFactType, HelpRequestFormat, MentorshipKind
from hmm.notifications.payloads import (
    AcceptReminderPayload,
    ActionKey,
    AdminAlertPayload,
    AdminDigestPayload,
    ApiAction,
    AttendanceConfirmDigestPayload,
    ContributionsConfirmDigestPayload,
    EventCancelledPayload,
    EventChangedPayload,
    FormationSoonPayload,
    HelpOfferAcceptedPayload,
    HelpOfferClosedPayload,
    HelpOfferReceivedPayload,
    HelpRequestDeclinedPayload,
    HelpRequestDirectPayload,
    HelpRequestExpiredPayload,
    HelpRequestMatchPayload,
    InviteAcceptedPayload,
    InviteClosedPayload,
    InviteDeclinedPayload,
    InviteExpiredPayload,
    JoinRequestAcceptedPayload,
    JoinRequestRejectedPayload,
    LeadAssignedPayload,
    LinkAction,
    MentorshipClosedPayload,
    MentorshipCloseProposedPayload,
    MentorshipIdlePayload,
    MentorshipStalledPayload,
    NotificationPayload,
    NotificationType,
    OnboardingReopenedPayload,
    PartyInvitePayload,
    PenaltyAppliedPayload,
    PlacementLapsedPayload,
    ProjectIdlePayload,
    ProjectInviteReceivedPayload,
    ProjectJoinRequestReceivedPayload,
    ProjectMemberJoinedPayload,
    ProjectMemberLeftPayload,
    ProjectMemberRemovedPayload,
    ProjectPausedPayload,
    ProjectReleasedPayload,
    ReservePlacedPayload,
    RestrictionLiftedPayload,
    ResultDuePayload,
    ReviewReceivedPayload,
    ReviewRemovedPayload,
    ReviewsOpenPayload,
    ReviewsPendingPayload,
    ScoreChange,
    ScoreChangedPayload,
    ScoresReviewedPayload,
    TeamDisbandedPayload,
    TeamInvitePayload,
    TeamMemberJoinedPayload,
    TeamMemberLeftPayload,
    TeamProposedPayload,
    TeamStatusChangedPayload,
    TelegramLinkBrokenPayload,
)

API_LABELS: dict[ActionKey, str] = {
    ActionKey.INVITE_ACCEPT: "Accept",
    ActionKey.INVITE_DECLINE: "Decline",
    ActionKey.JOIN_REQUEST_ACCEPT: "Accept",
    ActionKey.JOIN_REQUEST_REJECT: "Reject",
    ActionKey.PLACEMENT_ACCEPT: "Accept",
    ActionKey.PLACEMENT_DECLINE: "Decline",
    ActionKey.EVENT_ENROLL: "Enroll again",
    ActionKey.HELP_OFFER_ACCEPT: "Accept offer",
    ActionKey.HELP_REQUEST_DECLINE: "Decline",
    ActionKey.HELP_REQUEST_OPEN: "Open to everyone",
    ActionKey.MENTORSHIP_ACKNOWLEDGE: "Acknowledge",
}

LINK_LABELS: dict[str, str] = {
    "notifications.actions.review": "Write reviews",
    "notifications.actions.endorse": "Endorse",
    "notifications.actions.reopen": "Ask again",
    "notifications.actions.help": "Help",
    "notifications.actions.check_in": "Check in",
    "notifications.actions.close_with_outcome": "Close",
    "notifications.actions.submit_result": "Submit result",
    "notifications.actions.open_project": "Open project",
    "notifications.actions.transfer": "Transfer lead",
    "notifications.actions.open_alert": "Open alert",
    "notifications.actions.confirm_attendees": "See everyone",
}

CONFIRM_LINKS = frozenset(
    {"notifications.actions.confirm_attendees", "notifications.actions.confirm_lead"}
)

OPEN_LABELS: dict[NotificationType, str] = {
    NotificationType.ADMIN_DIGEST: "Open admin",
    NotificationType.ONBOARDING_REOPENED: "Continue onboarding",
}

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

SEVERITY_ICONS: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: "🔹",
    AlertSeverity.WARNING: "⚠️",
    AlertSeverity.CRITICAL: "🚨",
}

KIND_LABELS: dict[MentorshipKind, str] = {
    MentorshipKind.MENTORING: "mentoring",
    MentorshipKind.STUDY_PAIR: "peer study",
}

FORMAT_LABELS: dict[HelpRequestFormat, str] = {
    HelpRequestFormat.ONLINE: "online",
    HelpRequestFormat.OFFLINE: "in person",
}

MAX_CONFIRM_BUTTONS = 20
FALLBACK_TEXT = "🔔 You have a new notification."

TEMPLATES: dict[NotificationType, Callable[[Any], str]] = {}


def template[P](kind: NotificationType) -> Callable[[Callable[[P], str]], Callable[[P], str]]:
    def register(body: Callable[[P], str]) -> Callable[[P], str]:
        TEMPLATES[kind] = body
        return body

    return register


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    markup: InlineKeyboardMarkup | None
    silent: bool = False


def score_changes(changes: tuple[ScoreChange, ...]) -> str:
    return "\n".join(
        f"• {esc(change.area_name)}: {score(change.previous_score)} → "
        f"{bold(score(change.score))} — {esc(clip(change.reason, 160))}"
        for change in changes
    )


def invite_target(
    payload: InviteAcceptedPayload
    | InviteDeclinedPayload
    | InviteExpiredPayload
    | InviteClosedPayload,
) -> str:
    if payload.project_name:
        return f"to {subject(payload.project_name)}"
    if payload.event_title:
        return f"to the party for {subject(payload.event_title)}"
    return "to the party"


@template(NotificationType.PARTY_INVITE)
def party_invite(p: PartyInvitePayload) -> str:
    return lines(
        f"👥 {person(p.actor)} invited you to their party for {subject(p.event_title)}.",
        f"Expires {when(p.expires_at)}.",
    )


@template(NotificationType.TEAM_INVITE)
def team_invite(p: TeamInvitePayload) -> str:
    return lines(
        f"👥 {person(p.actor)} invited you to team {subject(p.team_name)} "
        f"for {bold(p.event_title)}.",
        quote(p.message),
        f"Expires {when(p.expires_at)}.",
    )


@template(NotificationType.INVITE_ACCEPTED)
def invite_accepted(p: InviteAcceptedPayload) -> str:
    return f"✅ {person(p.invitee)} accepted the invite {invite_target(p)}."


@template(NotificationType.INVITE_DECLINED)
def invite_declined(p: InviteDeclinedPayload) -> str:
    return f"{person(p.invitee)} declined the invite {invite_target(p)}."


@template(NotificationType.INVITE_EXPIRED)
def invite_expired(p: InviteExpiredPayload) -> str:
    return f"⌛ The invite for {person(p.invitee)} {invite_target(p)} expired."


@template(NotificationType.INVITE_CLOSED)
def invite_closed(p: InviteClosedPayload) -> str:
    return f"The invite for {person(p.invitee)} {invite_target(p)} was closed: {reason(p.reason)}."


@template(NotificationType.JOIN_REQUEST_ACCEPTED)
def join_request_accepted(p: JoinRequestAcceptedPayload) -> str:
    return f"✅ {person(p.lead)} accepted your request to join {subject(p.project_name)}."


@template(NotificationType.JOIN_REQUEST_REJECTED)
def join_request_rejected(p: JoinRequestRejectedPayload) -> str:
    return f"{person(p.lead)} turned down your request to join {subject(p.project_name)}."


@template(NotificationType.FORMATION_SOON)
def formation_soon(p: FormationSoonPayload) -> str:
    return lines(
        f"⏳ Teams for {subject(p.event_title)} form {when(p.formation_at, p.timezone)}.",
        f"{headcount(p.enrolled_count)} enrolled so far, about {plural(p.team_count, 'team')}."
        if p.enrolled_count
        else None,
    )


@template(NotificationType.TEAM_PROPOSED)
def team_proposed(p: TeamProposedPayload) -> str:
    return lines(
        f"🧩 You've been placed on team {subject(p.team_name)} for {bold(p.event_title)}.",
        italic(p.proposal_reason),
        f"Team: {people(p.members)}" if p.members else None,
        f"Accept by {when(p.accept_by, p.timezone)}.",
    )


@template(NotificationType.RESERVE_PLACED)
def reserve_placed(p: ReservePlacedPayload) -> str:
    return (
        f"Teams for {subject(p.event_title)} are formed. You're in the reserve "
        f"({headcount(p.reserve_count)}) and get a place if a seat opens."
    )


@template(NotificationType.ACCEPT_REMINDER)
def accept_reminder(p: AcceptReminderPayload) -> str:
    return (
        f"⏰ Your place on {subject(p.team_name)} ({esc(p.event_title)}) is waiting. "
        f"Accept by {when(p.accept_by, p.timezone)}."
    )


@template(NotificationType.PLACEMENT_LAPSED)
def placement_lapsed(p: PlacementLapsedPayload) -> str:
    return lines(
        f"Your place on {subject(p.team_name)} ({esc(p.event_title)}) lapsed: {reason(p.reason)}.",
        f"You can enroll again until {when(p.reg_deadline)}.",
    )


@template(NotificationType.TEAM_MEMBER_JOINED)
def team_member_joined(p: TeamMemberJoinedPayload) -> str:
    return (
        f"{person(p.member)} joined {subject(p.team_name)}. Now {plural(p.member_count, 'member')}."
    )


@template(NotificationType.TEAM_MEMBER_LEFT)
def team_member_left(p: TeamMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} left {subject(p.team_name)}: {reason(p.reason)}. "
        f"Now {plural(p.member_count, 'member')}."
    )


@template(NotificationType.LEAD_ASSIGNED)
def lead_assigned(p: LeadAssignedPayload) -> str:
    return f"👑 {person(p.lead)} now leads {subject(p.team_name)}: {reason(p.reason)}."


@template(NotificationType.TEAM_DISBANDED)
def team_disbanded(p: TeamDisbandedPayload) -> str:
    return f"Team {subject(p.team_name)} ({esc(p.event_title)}) was disbanded: {reason(p.reason)}."


@template(NotificationType.TEAM_STATUS_CHANGED)
def team_status_changed(p: TeamStatusChangedPayload) -> str:
    return (
        f"Team {subject(p.team_name)} is now {bold(humanize(p.status))} "
        f"({reason(p.reason)}). Good luck at {esc(p.event_title)}!"
    )


@template(NotificationType.EVENT_CHANGED)
def event_changed(p: EventChangedPayload) -> str:
    return lines(
        f"📅 {subject(p.event_title)} changed:",
        *(
            f"• {esc(humanize(change.field))}: {moment_or_text(change.previous, p.timezone)} → "
            f"{moment_or_text(change.current, p.timezone)}"
            for change in p.changes
        ),
        f"Now {span(p.starts_at, p.ends_at, p.timezone)}.",
    )


@template(NotificationType.EVENT_CANCELLED)
def event_cancelled(p: EventCancelledPayload) -> str:
    return f"❌ {subject(p.event_title)} was cancelled: {reason(p.reason)}."


@template(NotificationType.ATTENDANCE_CONFIRM_DIGEST)
def attendance_confirm_digest(p: AttendanceConfirmDigestPayload) -> str:
    return lines(
        f"Who did you meet at {subject(p.event_title)}?",
        "Tap everyone you actually met there." if p.attendees else None,
    )


@template(NotificationType.PROJECT_INVITE_RECEIVED)
def project_invite_received(p: ProjectInviteReceivedPayload) -> str:
    return lines(
        f"👥 {person(p.actor)} invited you to project {subject(p.project_name)}.",
        quote(p.message),
        f"Expires {when(p.expires_at)}.",
    )


@template(NotificationType.PROJECT_JOIN_REQUEST_RECEIVED)
def project_join_request_received(p: ProjectJoinRequestReceivedPayload) -> str:
    return lines(
        f"🙋 {person(p.requester)} wants to join {subject(p.project_name)}.", quote(p.note)
    )


@template(NotificationType.PROJECT_MEMBER_JOINED)
def project_member_joined(p: ProjectMemberJoinedPayload) -> str:
    return (
        f"{person(p.member)} joined {subject(p.project_name)}. "
        f"Now {plural(p.member_count, 'member')}."
    )


@template(NotificationType.PROJECT_MEMBER_LEFT)
def project_member_left(p: ProjectMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} left {subject(p.project_name)}. "
        f"Now {plural(p.member_count, 'member')}."
    )


@template(NotificationType.PROJECT_MEMBER_REMOVED)
def project_member_removed(p: ProjectMemberRemovedPayload) -> str:
    return lines(
        f"{person(p.actor)} removed {person(p.member)} from {subject(p.project_name)}.",
        quote(p.reason),
    )


@template(NotificationType.PROJECT_IDLE)
def project_idle(p: ProjectIdlePayload) -> str:
    return (
        f"💤 {subject(p.project_name)} has had no progress for {plural(p.idle_days, 'day')}. "
        f"It pauses {when(p.pauses_at)} unless someone posts an update."
    )


@template(NotificationType.PROJECT_PAUSED)
def project_paused(p: ProjectPausedPayload) -> str:
    return f"⏸ {subject(p.project_name)} was paused: {reason(p.reason)}."


@template(NotificationType.PROJECT_RELEASED)
def project_released(p: ProjectReleasedPayload) -> str:
    urls = [
        link(url, label)
        for url, label in (
            (p.repo_url, "repo"),
            (p.demo_url, "demo"),
            (p.writeup_url, "write-up"),
        )
        if url
    ]
    return lines(
        f"🚀 {person(p.actor)} released {bold(p.release_title)} of {subject(p.project_name)}.",
        quote(p.notes_md, 600),
        " · ".join(urls) if urls else None,
    )


@template(NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST)
def contributions_confirm_digest(p: ContributionsConfirmDigestPayload) -> str:
    return (
        f"{subject(p.project_name)} released {bold(p.release_title)}. "
        f"Can you confirm that {person(p.lead)} leads the project?"
    )


def help_request_lines(
    p: HelpRequestMatchPayload
    | HelpRequestDirectPayload
    | HelpRequestDeclinedPayload
    | HelpRequestExpiredPayload,
) -> list[str | None]:
    form = FORMAT_LABELS.get(p.format)
    return [
        f"{subject(p.topic)} · {esc(p.area_name)}" + (f" · {form}" if form else ""),
        quote(p.goal),
    ]


@template(NotificationType.HELP_REQUEST_MATCH)
def help_request_match(p: HelpRequestMatchPayload) -> str:
    return lines(
        f"🙋 {person(p.learner)} is looking for help:",
        *help_request_lines(p),
        italic(p.reason),
        f"Open until {when(p.expires_at)}.",
    )


@template(NotificationType.HELP_REQUEST_DIRECT)
def help_request_direct(p: HelpRequestDirectPayload) -> str:
    return lines(
        f"🙋 {person(p.learner)} asked you directly for help:",
        *help_request_lines(p),
        f"Respond by {when(p.respond_by)}.",
    )


@template(NotificationType.HELP_REQUEST_DECLINED)
def help_request_declined(p: HelpRequestDeclinedPayload) -> str:
    return lines(
        "The mentor you asked declined your help request:",
        *help_request_lines(p),
        "You can open it to everyone now.",
    )


def mentor_facts(p: HelpOfferReceivedPayload) -> str:
    facts = [KIND_LABELS[p.kind]]
    if p.mentor_score > 0:
        facts.append(f"{p.area_name} {score(p.mentor_score)} (you: {score(p.learner_score)})")
    if p.mentor_rating is not None:
        facts.append(f"rated {score(p.mentor_rating)} by {headcount(p.mentor_review_count)}")
    if p.past_mentorship_count:
        facts.append(plural(p.past_mentorship_count, "past mentorship"))
    if p.mentor_rating is None and not p.past_mentorship_count:
        facts.append("new mentor")
    return " · ".join(facts)


@template(NotificationType.HELP_OFFER_RECEIVED)
def help_offer_received(p: HelpOfferReceivedPayload) -> str:
    return lines(
        f"🤝 {person(p.mentor)} offered to help with {subject(p.topic)}:",
        quote(p.message),
        italic(mentor_facts(p)),
    )


@template(NotificationType.HELP_OFFER_ACCEPTED)
def help_offer_accepted(p: HelpOfferAcceptedPayload) -> str:
    return (
        f"🎉 {person(p.learner)} accepted your offer for {subject(p.topic)}. "
        "Your mentorship has started."
    )


@template(NotificationType.HELP_OFFER_CLOSED)
def help_offer_closed(p: HelpOfferClosedPayload) -> str:
    return f"Your offer for {subject(p.topic)} was closed: {reason(p.reason)}."


@template(NotificationType.HELP_REQUEST_EXPIRED)
def help_request_expired(p: HelpRequestExpiredPayload) -> str:
    return lines("⌛ Your help request expired without a match:", *help_request_lines(p))


@template(NotificationType.MENTORSHIP_IDLE)
def mentorship_idle(p: MentorshipIdlePayload) -> str:
    return (
        f"No check-ins on {subject(p.topic)} for {plural(p.idle_days, 'day')}. "
        f"It stalls {when(p.stalls_at)} without one."
    )


@template(NotificationType.MENTORSHIP_STALLED)
def mentorship_stalled(p: MentorshipStalledPayload) -> str:
    return (
        f"{subject(p.topic)} stalled after {plural(p.idle_days, 'quiet day')}. "
        f"It closes {when(p.closes_at)} unless someone checks in."
    )


@template(NotificationType.MENTORSHIP_CLOSE_PROPOSED)
def mentorship_close_proposed(p: MentorshipCloseProposedPayload) -> str:
    return (
        f"{person(p.mentor)} proposed closing {subject(p.topic)}. "
        f"Close it with an outcome by {when(p.respond_by)}."
    )


@template(NotificationType.MENTORSHIP_CLOSED)
def mentorship_closed(p: MentorshipClosedPayload) -> str:
    return lines(
        f"🏁 {subject(p.topic)} with {person(p.learner)} and {person(p.mentor)} is closed.",
        f"Outcome: {bold(humanize(p.outcome))}" if p.outcome else None,
        quote(p.summary),
        f"Reviews are open until {when(p.review_closes_at)}.",
    )


@template(NotificationType.PENALTY_APPLIED)
def penalty_applied(p: PenaltyAppliedPayload) -> str:
    return lines(
        f"⚠️ Penalty: {reason(p.reason)}.",
        f"XP {p.xp_delta}." if p.xp_delta else None,
        f"Restriction: {bold(humanize(p.restriction_kind))}"
        + (f" until {when(p.expires_at)}." if p.expires_at else ".")
        if p.restriction_kind
        else None,
    )


@template(NotificationType.RESTRICTION_LIFTED)
def restriction_lifted(p: RestrictionLiftedPayload) -> str:
    return f"Your {bold(humanize(p.restriction_kind))} restriction was lifted: {reason(p.reason)}."


@template(NotificationType.RESULT_DUE)
def result_due(p: ResultDuePayload) -> str:
    return lines(
        f"📝 Submit the result for {subject(p.team_name)} ({esc(p.event_title)}).",
        f"The lead can submit now; any member can from {when(p.member_submit_at)}.",
        f"Submissions close {when(p.closes_at)}.",
    )


def reviews_lines(p: ReviewsOpenPayload | ReviewsPendingPayload) -> list[str | None]:
    return [
        f"To review: {people(p.to_review)}" if p.to_review else None,
        f"{p.submitted_count} submitted so far." if p.submitted_count else None,
        f"Closes {when(p.closes_at)}.",
    ]


@template(NotificationType.REVIEWS_OPEN)
def reviews_open(p: ReviewsOpenPayload) -> str:
    return lines(f"⭐ Reviews are open for {subject(p.subject_title)}.", *reviews_lines(p))


@template(NotificationType.REVIEWS_PENDING)
def reviews_pending(p: ReviewsPendingPayload) -> str:
    return lines(
        f"⭐ Reminder: you still have reviews to write for {subject(p.subject_title)}.",
        *reviews_lines(p),
    )


@template(NotificationType.REVIEW_RECEIVED)
def review_received(p: ReviewReceivedPayload) -> str:
    return f"⭐ You received {plural(p.review_count, 'review')} for {subject(p.subject_title)}."


@template(NotificationType.REVIEW_REMOVED)
def review_removed(p: ReviewRemovedPayload) -> str:
    return (
        f"A review of {person(p.reviewee)} for {subject(p.subject_title)} was removed: "
        f"{reason(p.reason)}."
    )


@template(NotificationType.SCORE_CHANGED)
def score_changed(p: ScoreChangedPayload) -> str:
    return lines("📈 Your scores changed:", score_changes(p.changes))


@template(NotificationType.SCORES_REVIEWED)
def scores_reviewed(p: ScoresReviewedPayload) -> str:
    return lines(
        f"✅ {person(p.reviewer)} reviewed your scores.",
        score_changes(p.changes) if p.changes else "No changes.",
    )


@template(NotificationType.ONBOARDING_REOPENED)
def onboarding_reopened(p: OnboardingReopenedPayload) -> str:
    return lines(f"{person(p.actor)} reopened your onboarding.", quote(p.reason))


@template(NotificationType.TELEGRAM_LINK_BROKEN)
def telegram_link_broken(p: TelegramLinkBrokenPayload) -> str:
    return f"Telegram delivery stopped {when(p.broken_at)}: {reason(p.reason)}."


@template(NotificationType.ADMIN_ALERT)
def admin_alert(p: AdminAlertPayload) -> str:
    alert = p.alert
    return lines(
        f"{SEVERITY_ICONS[alert.severity]} {subject(alert.title)}",
        esc(clip(alert.message, 800)),
        italic(f"{humanize(alert.type)} · {alert.severity} · {alert.subject_title}"),
    )


@template(NotificationType.ADMIN_DIGEST)
def admin_digest(p: AdminDigestPayload) -> str:
    counts = [
        (p.pending_member_reviews, "member review", " waiting"),
        (p.overdue_to_check_items, "overdue to-check item", ""),
        (p.member_event_duplicates, "member event duplicate", ""),
        (p.results_to_verify, "result", " to verify"),
        *((count, f"open {humanize(kind)} alert", "") for kind, count in p.open_alerts.items()),
    ]
    items = [f"• {esc(plural(count, noun))}{suffix}" for count, noun, suffix in counts if count]
    return lines(
        f"🗂 Admin digest, {day(p.period_starts_at)} to {day(p.period_ends_at)}",
        *(items or ["Nothing waiting."]),
    )


def native_button(action: LinkAction, payload: NotificationPayload) -> InlineKeyboardButton | None:
    label = LINK_LABELS.get(action.label_key, "")
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
                    text=f"☐ Confirm {clip(payload.lead.display_name, 40)}",
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
            LINK_LABELS.get(action.label_key)
            or humanize(action.label_key.rsplit(".", 1)[-1]).capitalize()
        )
        links.append((action.url, label))
    open_label = OPEN_LABELS.get(payload.type)
    if open_label is not None and all(path != payload.link for path, _ in links):
        links.append((payload.link, open_label))
    return links


def render(
    payload: NotificationPayload,
    public_url: str,
    *,
    now: datetime | None = None,
    timezone: str | None = None,
) -> Rendered:
    with viewing(View(public_url=public_url, now=now, timezone=timezone, link=payload.link)):
        body = TEMPLATES.get(payload.type)
        text = body(payload) if body is not None else FALLBACK_TEXT
    api_row = [
        InlineKeyboardButton(
            text=API_LABELS[action.key],
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
