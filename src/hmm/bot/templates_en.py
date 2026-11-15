from hmm.bot.fmt import (
    bold,
    change_line,
    clip,
    day,
    esc,
    headcount,
    italic,
    lines,
    link,
    people,
    person,
    plural,
    quote,
    reason,
    score,
    score_changes,
    span,
    subject,
    tr,
    when,
)
from hmm.bot.i18n import SEVERITY_ICONS, Noun, registrar
from hmm.notifications.payloads import (
    AcceptReminderPayload,
    AdminAlertPayload,
    AdminDigestPayload,
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
    MentorshipClosedPayload,
    MentorshipCloseProposedPayload,
    MentorshipIdlePayload,
    MentorshipStalledPayload,
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

TEMPLATES, template = registrar()

TEAM = Noun(("team", "teams"))
MEMBER = Noun(("member", "members"))
DAY = Noun(("day", "days"))
QUIET_DAY = Noun(("quiet day", "quiet days"))
REVIEW = Noun(("review", "reviews"))
PAST_MENTORSHIP = Noun(("past mentorship", "past mentorships"))


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
        f"{headcount(p.enrolled_count)} enrolled so far"
        + (f", about {plural(p.team_count, TEAM)}." if p.team_count else ".")
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
        f"{person(p.member)} joined {subject(p.team_name)}. Now {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.TEAM_MEMBER_LEFT)
def team_member_left(p: TeamMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} left {subject(p.team_name)}: {reason(p.reason)}. "
        f"Now {plural(p.member_count, MEMBER)}."
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
        f"Team {subject(p.team_name)} is now {bold(tr().team_statuses[p.status])} "
        f"({reason(p.reason)}). Good luck at {esc(p.event_title)}!"
    )


@template(NotificationType.EVENT_CHANGED)
def event_changed(p: EventChangedPayload) -> str:
    return lines(
        f"📅 {subject(p.event_title)} changed:",
        *(change_line(change, p.timezone) for change in p.changes),
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
        f"Now {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.PROJECT_MEMBER_LEFT)
def project_member_left(p: ProjectMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} left {subject(p.project_name)}. Now {plural(p.member_count, MEMBER)}."
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
        f"💤 {subject(p.project_name)} has had no progress for {plural(p.idle_days, DAY)}. "
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
    form = tr().help_formats.get(p.format)
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
    facts = [tr().mentorship_kinds[p.kind]]
    if p.mentor_score > 0:
        facts.append(f"{p.area_name} {score(p.mentor_score)} (you: {score(p.learner_score)})")
    if p.mentor_rating is not None:
        facts.append(f"rated {score(p.mentor_rating)} by {headcount(p.mentor_review_count)}")
    if p.past_mentorship_count:
        facts.append(plural(p.past_mentorship_count, PAST_MENTORSHIP))
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
        f"No check-ins on {subject(p.topic)} for {plural(p.idle_days, DAY)}. "
        f"It stalls {when(p.stalls_at)} without one."
    )


@template(NotificationType.MENTORSHIP_STALLED)
def mentorship_stalled(p: MentorshipStalledPayload) -> str:
    return (
        f"{subject(p.topic)} stalled after {plural(p.idle_days, QUIET_DAY)}. "
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
        f"Outcome: {bold(tr().outcomes[p.outcome])}" if p.outcome else None,
        quote(p.summary),
        f"Reviews are open until {when(p.review_closes_at)}.",
    )


@template(NotificationType.PENALTY_APPLIED)
def penalty_applied(p: PenaltyAppliedPayload) -> str:
    return lines(
        f"⚠️ Penalty: {reason(p.reason)}.",
        f"XP {p.xp_delta}." if p.xp_delta else None,
        f"Restriction: {bold(tr().restriction_kinds[p.restriction_kind])}"
        + (f" until {when(p.expires_at)}." if p.expires_at else ".")
        if p.restriction_kind
        else None,
    )


@template(NotificationType.RESTRICTION_LIFTED)
def restriction_lifted(p: RestrictionLiftedPayload) -> str:
    kind = tr().restriction_kinds[p.restriction_kind]
    return f"Your {bold(kind)} restriction was lifted: {reason(p.reason)}."


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
    return f"⭐ You received {plural(p.review_count, REVIEW)} for {subject(p.subject_title)}."


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
        italic(f"{tr().alert_kinds[alert.type]} · {alert.subject_title}"),
    )


def alert_noun(label: str) -> Noun:
    return Noun((f"open {label} alert", f"open {label} alerts"))


@template(NotificationType.ADMIN_DIGEST)
def admin_digest(p: AdminDigestPayload) -> str:
    counts = [
        (p.pending_member_reviews, Noun(("member review", "member reviews")), " waiting"),
        (p.overdue_to_check_items, Noun(("overdue to-check item", "overdue to-check items")), ""),
        (
            p.member_event_duplicates,
            Noun(("member event duplicate", "member event duplicates")),
            "",
        ),
        (p.results_to_verify, Noun(("result", "results")), " to verify"),
        *((count, alert_noun(tr().alert_kinds[kind]), "") for kind, count in p.open_alerts.items()),
    ]
    items = [f"• {esc(plural(count, noun))}{suffix}" for count, noun, suffix in counts if count]
    return lines(
        f"🗂 Admin digest, {day(p.period_starts_at)} to {day(p.period_ends_at)}",
        *(items or ["Nothing waiting."]),
    )
