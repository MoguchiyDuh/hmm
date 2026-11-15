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

MEMBER = Noun(("участник", "участника", "участников"))
DAY = Noun(("день", "дня", "дней"))
REVIEW = Noun(("отзыв", "отзыва", "отзывов"))


def invite_target(
    payload: InviteAcceptedPayload
    | InviteDeclinedPayload
    | InviteExpiredPayload
    | InviteClosedPayload,
) -> str:
    if payload.project_name:
        return f"в {subject(payload.project_name)}"
    if payload.event_title:
        return f"в группу на {subject(payload.event_title)}"
    return "в группу"


@template(NotificationType.PARTY_INVITE)
def party_invite(p: PartyInvitePayload) -> str:
    return lines(
        f"👥 {person(p.actor)} приглашает вас в свою группу на {subject(p.event_title)}.",
        f"Действует до {when(p.expires_at)}.",
    )


@template(NotificationType.TEAM_INVITE)
def team_invite(p: TeamInvitePayload) -> str:
    return lines(
        f"👥 {person(p.actor)} приглашает вас в команду {subject(p.team_name)} "
        f"на {bold(p.event_title)}.",
        quote(p.message),
        f"Действует до {when(p.expires_at)}.",
    )


@template(NotificationType.INVITE_ACCEPTED)
def invite_accepted(p: InviteAcceptedPayload) -> str:
    return f"✅ Участник {person(p.invitee)} принял приглашение {invite_target(p)}."


@template(NotificationType.INVITE_DECLINED)
def invite_declined(p: InviteDeclinedPayload) -> str:
    return f"Участник {person(p.invitee)} отклонил приглашение {invite_target(p)}."


@template(NotificationType.INVITE_EXPIRED)
def invite_expired(p: InviteExpiredPayload) -> str:
    return f"⌛ Истекло приглашение {invite_target(p)} для {person(p.invitee)}."


@template(NotificationType.INVITE_CLOSED)
def invite_closed(p: InviteClosedPayload) -> str:
    return f"Приглашение {invite_target(p)} для {person(p.invitee)} закрыто: {reason(p.reason)}."


@template(NotificationType.JOIN_REQUEST_ACCEPTED)
def join_request_accepted(p: JoinRequestAcceptedPayload) -> str:
    return f"✅ Лид {person(p.lead)} принял вашу заявку на вступление в {subject(p.project_name)}."


@template(NotificationType.JOIN_REQUEST_REJECTED)
def join_request_rejected(p: JoinRequestRejectedPayload) -> str:
    return f"Лид {person(p.lead)} отклонил вашу заявку на вступление в {subject(p.project_name)}."


@template(NotificationType.FORMATION_SOON)
def formation_soon(p: FormationSoonPayload) -> str:
    return lines(
        f"⏳ Формирование команд на {subject(p.event_title)}: {when(p.formation_at, p.timezone)}.",
        f"Уже записались: {headcount(p.enrolled_count)}"
        + (f", это примерно {plural(p.team_count, tr().team)}." if p.team_count else ".")
        if p.enrolled_count
        else None,
    )


@template(NotificationType.TEAM_PROPOSED)
def team_proposed(p: TeamProposedPayload) -> str:
    return lines(
        f"🧩 Вы распределены в команду {subject(p.team_name)} на {bold(p.event_title)}.",
        italic(p.proposal_reason),
        f"Команда: {people(p.members)}" if p.members else None,
        f"Подтвердите место до {when(p.accept_by, p.timezone)}.",
    )


@template(NotificationType.RESERVE_PLACED)
def reserve_placed(p: ReservePlacedPayload) -> str:
    return (
        f"Команды на {subject(p.event_title)} сформированы. Вы в резерве "
        f"(всего {headcount(p.reserve_count)}) и получите место, если оно освободится."
    )


@template(NotificationType.ACCEPT_REMINDER)
def accept_reminder(p: AcceptReminderPayload) -> str:
    return (
        f"⏰ Ваше место в команде {subject(p.team_name)} ({esc(p.event_title)}) "
        f"ждёт подтверждения. Подтвердите до {when(p.accept_by, p.timezone)}."
    )


@template(NotificationType.PLACEMENT_LAPSED)
def placement_lapsed(p: PlacementLapsedPayload) -> str:
    return lines(
        f"Ваше место в команде {subject(p.team_name)} ({esc(p.event_title)}) аннулировано: "
        f"{reason(p.reason)}.",
        f"Встать в очередь снова можно до {when(p.reg_deadline)}.",
    )


@template(NotificationType.TEAM_MEMBER_JOINED)
def team_member_joined(p: TeamMemberJoinedPayload) -> str:
    return (
        f"{person(p.member)} теперь в команде {subject(p.team_name)}. "
        f"Всего {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.TEAM_MEMBER_LEFT)
def team_member_left(p: TeamMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} больше не в команде {subject(p.team_name)}: {reason(p.reason)}. "
        f"Осталось {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.LEAD_ASSIGNED)
def lead_assigned(p: LeadAssignedPayload) -> str:
    return (
        f"👑 Новый лид команды {subject(p.team_name)}: {person(p.lead)}. "
        f"Причина: {reason(p.reason)}."
    )


@template(NotificationType.TEAM_DISBANDED)
def team_disbanded(p: TeamDisbandedPayload) -> str:
    return (
        f"Команда {subject(p.team_name)} ({esc(p.event_title)}) расформирована: {reason(p.reason)}."
    )


@template(NotificationType.TEAM_STATUS_CHANGED)
def team_status_changed(p: TeamStatusChangedPayload) -> str:
    return (
        f"Команда {subject(p.team_name)} теперь {bold(tr().team_statuses[p.status])} "
        f"({reason(p.reason)}). Удачи на {esc(p.event_title)}!"
    )


@template(NotificationType.EVENT_CHANGED)
def event_changed(p: EventChangedPayload) -> str:
    return lines(
        f"📅 Изменения в {subject(p.event_title)}:",
        *(change_line(change, p.timezone) for change in p.changes),
        f"Теперь: {span(p.starts_at, p.ends_at, p.timezone)}.",
    )


@template(NotificationType.EVENT_CANCELLED)
def event_cancelled(p: EventCancelledPayload) -> str:
    return f"❌ Событие {subject(p.event_title)} отменено: {reason(p.reason)}."


@template(NotificationType.ATTENDANCE_CONFIRM_DIGEST)
def attendance_confirm_digest(p: AttendanceConfirmDigestPayload) -> str:
    return lines(
        f"Кого вы встретили на {subject(p.event_title)}?",
        "Отметьте всех, кого вы действительно там встретили." if p.attendees else None,
    )


@template(NotificationType.PROJECT_INVITE_RECEIVED)
def project_invite_received(p: ProjectInviteReceivedPayload) -> str:
    return lines(
        f"👥 {person(p.actor)} приглашает вас в проект {subject(p.project_name)}.",
        quote(p.message),
        f"Действует до {when(p.expires_at)}.",
    )


@template(NotificationType.PROJECT_JOIN_REQUEST_RECEIVED)
def project_join_request_received(p: ProjectJoinRequestReceivedPayload) -> str:
    return lines(
        f"🙋 {person(p.requester)} хочет вступить в {subject(p.project_name)}.", quote(p.note)
    )


@template(NotificationType.PROJECT_MEMBER_JOINED)
def project_member_joined(p: ProjectMemberJoinedPayload) -> str:
    return (
        f"{person(p.member)} теперь в проекте {subject(p.project_name)}. "
        f"Всего {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.PROJECT_MEMBER_LEFT)
def project_member_left(p: ProjectMemberLeftPayload) -> str:
    return (
        f"{person(p.member)} больше не в проекте {subject(p.project_name)}. "
        f"Осталось {plural(p.member_count, MEMBER)}."
    )


@template(NotificationType.PROJECT_MEMBER_REMOVED)
def project_member_removed(p: ProjectMemberRemovedPayload) -> str:
    return lines(
        f"Участник {person(p.member)} удалён из проекта {subject(p.project_name)}. "
        f"Инициатор: {person(p.actor)}.",
        quote(p.reason),
    )


@template(NotificationType.PROJECT_IDLE)
def project_idle(p: ProjectIdlePayload) -> str:
    return (
        f"💤 В проекте {subject(p.project_name)} нет прогресса уже {plural(p.idle_days, DAY)}. "
        f"Он встанет на паузу {when(p.pauses_at)}, если никто не опубликует обновление."
    )


@template(NotificationType.PROJECT_PAUSED)
def project_paused(p: ProjectPausedPayload) -> str:
    return f"⏸ Проект {subject(p.project_name)} на паузе: {reason(p.reason)}."


@template(NotificationType.PROJECT_RELEASED)
def project_released(p: ProjectReleasedPayload) -> str:
    urls = [
        link(url, label)
        for url, label in (
            (p.repo_url, "репозиторий"),
            (p.demo_url, "демо"),
            (p.writeup_url, "описание"),
        )
        if url
    ]
    return lines(
        f"🚀 Вышел релиз {bold(p.release_title)} проекта {subject(p.project_name)}. "
        f"Автор: {person(p.actor)}.",
        quote(p.notes_md, 600),
        " · ".join(urls) if urls else None,
    )


@template(NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST)
def contributions_confirm_digest(p: ContributionsConfirmDigestPayload) -> str:
    return (
        f"{subject(p.project_name)}: вышел релиз {bold(p.release_title)}. "
        f"Подтверждаете, что {person(p.lead)} руководит проектом?"
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
        f"🙋 {person(p.learner)} ищет помощь:",
        *help_request_lines(p),
        italic(p.reason),
        f"Запрос открыт до {when(p.expires_at)}.",
    )


@template(NotificationType.HELP_REQUEST_DIRECT)
def help_request_direct(p: HelpRequestDirectPayload) -> str:
    return lines(
        f"🙋 {person(p.learner)} просит о помощи именно вас:",
        *help_request_lines(p),
        f"Ответьте до {when(p.respond_by)}.",
    )


@template(NotificationType.HELP_REQUEST_DECLINED)
def help_request_declined(p: HelpRequestDeclinedPayload) -> str:
    return lines(
        "Ментор, которого вы выбрали, отклонил ваш запрос помощи:",
        *help_request_lines(p),
        "Теперь его можно открыть для всех.",
    )


def mentor_facts(p: HelpOfferReceivedPayload) -> str:
    facts = [tr().mentorship_kinds[p.kind]]
    if p.mentor_score > 0:
        facts.append(f"{p.area_name} {score(p.mentor_score)} (у вас {score(p.learner_score)})")
    if p.mentor_rating is not None:
        facts.append(f"оценка {score(p.mentor_rating)}, отзывов: {p.mentor_review_count}")
    if p.past_mentorship_count:
        facts.append(f"прошлых менторств: {p.past_mentorship_count}")
    if p.mentor_rating is None and not p.past_mentorship_count:
        facts.append("новый ментор")
    return " · ".join(facts)


@template(NotificationType.HELP_OFFER_RECEIVED)
def help_offer_received(p: HelpOfferReceivedPayload) -> str:
    return lines(
        f"🤝 Ментор {person(p.mentor)} предлагает помощь по теме {subject(p.topic)}:",
        quote(p.message),
        italic(mentor_facts(p)),
    )


@template(NotificationType.HELP_OFFER_ACCEPTED)
def help_offer_accepted(p: HelpOfferAcceptedPayload) -> str:
    return (
        f"🎉 Ученик {person(p.learner)} принял ваше предложение по теме {subject(p.topic)}. "
        "Менторство началось."
    )


@template(NotificationType.HELP_OFFER_CLOSED)
def help_offer_closed(p: HelpOfferClosedPayload) -> str:
    return f"Ваше предложение по теме {subject(p.topic)} закрыто: {reason(p.reason)}."


@template(NotificationType.HELP_REQUEST_EXPIRED)
def help_request_expired(p: HelpRequestExpiredPayload) -> str:
    return lines(
        "⌛ Срок вашего запроса помощи истёк, подходящего ментора не нашлось:",
        *help_request_lines(p),
    )


@template(NotificationType.MENTORSHIP_IDLE)
def mentorship_idle(p: MentorshipIdlePayload) -> str:
    return (
        f"В менторстве по теме {subject(p.topic)} нет чек-инов уже {plural(p.idle_days, DAY)}. "
        f"Без чек-ина оно застопорится {when(p.stalls_at)}."
    )


@template(NotificationType.MENTORSHIP_STALLED)
def mentorship_stalled(p: MentorshipStalledPayload) -> str:
    return (
        f"Менторство по теме {subject(p.topic)} застопорилось: "
        f"{plural(p.idle_days, DAY)} без активности. "
        f"Оно закроется {when(p.closes_at)}, если никто не сделает чек-ин."
    )


@template(NotificationType.MENTORSHIP_CLOSE_PROPOSED)
def mentorship_close_proposed(p: MentorshipCloseProposedPayload) -> str:
    return (
        f"Ментор {person(p.mentor)} предлагает завершить менторство по теме {subject(p.topic)}. "
        f"Завершите его с итогом до {when(p.respond_by)}."
    )


@template(NotificationType.MENTORSHIP_CLOSED)
def mentorship_closed(p: MentorshipClosedPayload) -> str:
    return lines(
        f"🏁 Менторство по теме {subject(p.topic)} завершено. "
        f"Ученик: {person(p.learner)}, ментор: {person(p.mentor)}.",
        f"Итог: {bold(tr().outcomes[p.outcome])}" if p.outcome else None,
        quote(p.summary),
        f"Отзывы открыты до {when(p.review_closes_at)}.",
    )


@template(NotificationType.PENALTY_APPLIED)
def penalty_applied(p: PenaltyAppliedPayload) -> str:
    return lines(
        f"⚠️ Штраф: {reason(p.reason)}.",
        f"XP {p.xp_delta}." if p.xp_delta else None,
        f"Ограничение на {bold(tr().restriction_kinds[p.restriction_kind])}"
        + (f" до {when(p.expires_at)}." if p.expires_at else ".")
        if p.restriction_kind
        else None,
    )


@template(NotificationType.RESTRICTION_LIFTED)
def restriction_lifted(p: RestrictionLiftedPayload) -> str:
    kind = tr().restriction_kinds[p.restriction_kind]
    return f"Ограничение на {bold(kind)} снято: {reason(p.reason)}."


@template(NotificationType.RESULT_DUE)
def result_due(p: ResultDuePayload) -> str:
    return lines(
        f"📝 Отправьте результат команды {subject(p.team_name)} ({esc(p.event_title)}).",
        f"Лид может отправить его сейчас, любой участник — с {when(p.member_submit_at)}.",
        f"Приём закрывается {when(p.closes_at)}.",
    )


def reviews_lines(p: ReviewsOpenPayload | ReviewsPendingPayload) -> list[str | None]:
    return [
        f"Кому написать отзыв: {people(p.to_review)}" if p.to_review else None,
        f"Уже отправлено: {p.submitted_count}." if p.submitted_count else None,
        f"Отзывы закрываются {when(p.closes_at)}.",
    ]


@template(NotificationType.REVIEWS_OPEN)
def reviews_open(p: ReviewsOpenPayload) -> str:
    return lines(f"⭐ Открыты отзывы по {subject(p.subject_title)}.", *reviews_lines(p))


@template(NotificationType.REVIEWS_PENDING)
def reviews_pending(p: ReviewsPendingPayload) -> str:
    return lines(
        f"⭐ Напоминание: у вас остались ненаписанные отзывы по {subject(p.subject_title)}.",
        *reviews_lines(p),
    )


@template(NotificationType.REVIEW_RECEIVED)
def review_received(p: ReviewReceivedPayload) -> str:
    return f"⭐ Вы получили {plural(p.review_count, REVIEW)} по {subject(p.subject_title)}."


@template(NotificationType.REVIEW_REMOVED)
def review_removed(p: ReviewRemovedPayload) -> str:
    return f"Отзыв о {person(p.reviewee)} по {subject(p.subject_title)} удалён: {reason(p.reason)}."


@template(NotificationType.SCORE_CHANGED)
def score_changed(p: ScoreChangedPayload) -> str:
    return lines("📈 Ваши оценки изменились:", score_changes(p.changes))


@template(NotificationType.SCORES_REVIEWED)
def scores_reviewed(p: ScoresReviewedPayload) -> str:
    return lines(
        f"✅ Администратор {person(p.reviewer)} проверил ваши оценки.",
        score_changes(p.changes) if p.changes else "Изменений нет.",
    )


@template(NotificationType.ONBOARDING_REOPENED)
def onboarding_reopened(p: OnboardingReopenedPayload) -> str:
    return lines(f"Администратор {person(p.actor)} снова открыл ваш онбординг.", quote(p.reason))


@template(NotificationType.TELEGRAM_LINK_BROKEN)
def telegram_link_broken(p: TelegramLinkBrokenPayload) -> str:
    return f"Доставка в Telegram остановлена {when(p.broken_at)}: {reason(p.reason)}."


@template(NotificationType.ADMIN_ALERT)
def admin_alert(p: AdminAlertPayload) -> str:
    alert = p.alert
    return lines(
        f"{SEVERITY_ICONS[alert.severity]} {subject(alert.title)}",
        esc(clip(alert.message, 800)),
        italic(f"{tr().alert_kinds[alert.type]} · {alert.subject_title}"),
    )


@template(NotificationType.ADMIN_DIGEST)
def admin_digest(p: AdminDigestPayload) -> str:
    counts = [
        (p.pending_member_reviews, "Участники на проверке"),
        (p.overdue_to_check_items, "Просроченные задачи на проверку"),
        (p.member_event_duplicates, "Дубликаты событий от участников"),
        (p.results_to_verify, "Результаты на проверку"),
        *(
            (count, f"Открытые алерты «{tr().alert_kinds[kind]}»")
            for kind, count in p.open_alerts.items()
        ),
    ]
    items = [f"• {esc(label)}: {count}" for count, label in counts if count]
    return lines(
        f"🗂 Сводка для админов, {day(p.period_starts_at)} — {day(p.period_ends_at)}",
        *(items or ["Всё разобрано."]),
    )
