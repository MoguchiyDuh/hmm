from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, StringConstraints, model_validator

from hmm_contracts.common import (
    AdminAlertId,
    AlertSeverity,
    AlertStatus,
    AreaKey,
    AttendanceId,
    Contract,
    DeliveryId,
    DeliveryStatus,
    ErrorCode,
    EventId,
    HelpOfferId,
    HelpRequestFormat,
    HelpRequestId,
    InviteId,
    InviteKind,
    JoinRequestId,
    Locale,
    MentorshipId,
    MentorshipKind,
    MentorshipOutcome,
    NotificationChannel,
    NotificationId,
    PartyId,
    ProjectId,
    ProjectMemberId,
    ProjectStatus,
    ReleaseId,
    RestrictionId,
    RestrictionKind,
    ReviewContext,
    ReviewId,
    ReviewWindowId,
    ReviewWindowSubjectType,
    SubjectId,
    TeamStatus,
    TimeZoneName,
    UserId,
)

__all__ = [
    "ACTION_ROUTES",
    "ALERT_SEVERITIES",
    "ALLOWED_ACTIONS",
    "TELEGRAM_OFF_BY_DEFAULT",
    "AcceptReminderPayload",
    "ActionKey",
    "ActionRoute",
    "AdminAlert",
    "AdminAlertPayload",
    "AdminDigestPayload",
    "AlertKind",
    "ApiAction",
    "AttendanceConfirmDigestPayload",
    "AttendanceConfirmationCandidate",
    "ContributionsConfirmDigestPayload",
    "DeliveryAck",
    "DeliveryAckResult",
    "EventCancelledPayload",
    "EventChangedPayload",
    "FormationSoonPayload",
    "HelpOfferAcceptedPayload",
    "HelpOfferClosedPayload",
    "HelpOfferReceivedPayload",
    "HelpRequestDeclinedPayload",
    "HelpRequestDirectPayload",
    "HelpRequestExpiredPayload",
    "HelpRequestMatchPayload",
    "InviteAcceptedPayload",
    "InviteClosedPayload",
    "InviteDeclinedPayload",
    "InviteExpiredPayload",
    "JoinRequestAcceptedPayload",
    "JoinRequestRejectedPayload",
    "LeadAssignedPayload",
    "LinkAction",
    "MentorshipCloseProposedPayload",
    "MentorshipClosedPayload",
    "MentorshipIdlePayload",
    "MentorshipStalledPayload",
    "MentorshipStartedPayload",
    "Notification",
    "NotificationActionCallback",
    "NotificationActionResult",
    "NotificationChange",
    "NotificationPayload",
    "NotificationPerson",
    "NotificationType",
    "OnboardingReopenedPayload",
    "PartyInvitePayload",
    "PenaltyAppliedPayload",
    "PendingDeliveriesQuery",
    "PendingDelivery",
    "PlacementLapsedPayload",
    "ProjectIdlePayload",
    "ProjectInviteReceivedPayload",
    "ProjectJoinRequestReceivedPayload",
    "ProjectMemberJoinedPayload",
    "ProjectMemberLeftPayload",
    "ProjectMemberRemovedPayload",
    "ProjectPausedPayload",
    "ProjectReleasedPayload",
    "ReservePlacedPayload",
    "RestrictionLiftedPayload",
    "ResultDuePayload",
    "ReviewReceivedPayload",
    "ReviewRemovedPayload",
    "ReviewsOpenPayload",
    "ReviewsPendingPayload",
    "ScoreChange",
    "ScoreChangedPayload",
    "ScoresReviewedPayload",
    "TeamDisbandedPayload",
    "TeamInvitePayload",
    "TeamMemberJoinedPayload",
    "TeamMemberLeftPayload",
    "TeamProposedPayload",
    "TeamStatusChangedPayload",
    "TelegramLinkBrokenPayload",
    "TelegramLinkRequest",
    "TelegramLinkResult",
    "TelegramUnlinkRequest",
    "WebPath",
]

type WebPath = Annotated[str, StringConstraints(pattern=r"^/([^/\s][^\s]*)?$")]


class NotificationType(StrEnum):
    PARTY_INVITE = "party_invite"
    TEAM_INVITE = "team_invite"
    INVITE_ACCEPTED = "invite_accepted"
    INVITE_DECLINED = "invite_declined"
    INVITE_EXPIRED = "invite_expired"
    INVITE_CLOSED = "invite_closed"
    JOIN_REQUEST_ACCEPTED = "join_request_accepted"
    JOIN_REQUEST_REJECTED = "join_request_rejected"
    FORMATION_SOON = "formation_soon"
    TEAM_PROPOSED = "team_proposed"
    RESERVE_PLACED = "reserve_placed"
    ACCEPT_REMINDER = "accept_reminder"
    PLACEMENT_LAPSED = "placement_lapsed"
    TEAM_MEMBER_JOINED = "team_member_joined"
    TEAM_MEMBER_LEFT = "team_member_left"
    LEAD_ASSIGNED = "lead_assigned"
    TEAM_DISBANDED = "team_disbanded"
    TEAM_STATUS_CHANGED = "team_status_changed"
    EVENT_CHANGED = "event_changed"
    EVENT_CANCELLED = "event_cancelled"
    ATTENDANCE_CONFIRM_DIGEST = "attendance_confirm_digest"
    PROJECT_INVITE_RECEIVED = "project_invite_received"
    PROJECT_JOIN_REQUEST_RECEIVED = "project_join_request_received"
    PROJECT_MEMBER_JOINED = "project_member_joined"
    PROJECT_MEMBER_LEFT = "project_member_left"
    PROJECT_MEMBER_REMOVED = "project_member_removed"
    PROJECT_IDLE = "project_idle"
    PROJECT_PAUSED = "project_paused"
    PROJECT_RELEASED = "project_released"
    CONTRIBUTIONS_CONFIRM_DIGEST = "contributions_confirm_digest"
    HELP_REQUEST_MATCH = "help_request_match"
    HELP_REQUEST_DIRECT = "help_request_direct"
    HELP_REQUEST_DECLINED = "help_request_declined"
    HELP_OFFER_RECEIVED = "help_offer_received"
    HELP_OFFER_ACCEPTED = "help_offer_accepted"
    HELP_OFFER_CLOSED = "help_offer_closed"
    HELP_REQUEST_EXPIRED = "help_request_expired"
    MENTORSHIP_STARTED = "mentorship_started"
    MENTORSHIP_IDLE = "mentorship_idle"
    MENTORSHIP_STALLED = "mentorship_stalled"
    MENTORSHIP_CLOSE_PROPOSED = "mentorship_close_proposed"
    MENTORSHIP_CLOSED = "mentorship_closed"
    PENALTY_APPLIED = "penalty_applied"
    RESTRICTION_LIFTED = "restriction_lifted"
    RESULT_DUE = "result_due"
    REVIEWS_OPEN = "reviews_open"
    REVIEWS_PENDING = "reviews_pending"
    REVIEW_RECEIVED = "review_received"
    REVIEW_REMOVED = "review_removed"
    SCORE_CHANGED = "score_changed"
    SCORES_REVIEWED = "scores_reviewed"
    ONBOARDING_REOPENED = "onboarding_reopened"
    TELEGRAM_LINK_BROKEN = "telegram_link_broken"
    ADMIN_ALERT = "admin_alert"
    ADMIN_DIGEST = "admin_digest"


class ActionKey(StrEnum):
    INVITE_ACCEPT = "invite_accept"
    INVITE_DECLINE = "invite_decline"
    JOIN_REQUEST_ACCEPT = "join_request_accept"
    JOIN_REQUEST_REJECT = "join_request_reject"
    PLACEMENT_ACCEPT = "placement_accept"
    PLACEMENT_DECLINE = "placement_decline"
    EVENT_ENROLL = "event_enroll"
    HELP_OFFER_ACCEPT = "help_offer_accept"
    HELP_REQUEST_DECLINE = "help_request_decline"
    HELP_REQUEST_OPEN = "help_request_open"
    MENTORSHIP_ACKNOWLEDGE = "mentorship_acknowledge"


class ActionRoute(Contract):
    method: Literal["POST"]
    path: WebPath
    body: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not self.path.startswith("/api/v1/") or self.path.count("{id}") != 1:
            raise ValueError("Action route must have an API path with exactly one {id}")
        remainder = self.path.replace("{id}", "")
        if "{" in remainder or "}" in remainder:
            raise ValueError("Action route cannot have other placeholders")
        return self


ACTION_ROUTES: dict[ActionKey, ActionRoute] = {
    ActionKey.INVITE_ACCEPT: ActionRoute(method="POST", path="/api/v1/invites/{id}/accept"),
    ActionKey.INVITE_DECLINE: ActionRoute(method="POST", path="/api/v1/invites/{id}/decline"),
    ActionKey.JOIN_REQUEST_ACCEPT: ActionRoute(
        method="POST", path="/api/v1/join-requests/{id}/accept"
    ),
    ActionKey.JOIN_REQUEST_REJECT: ActionRoute(
        method="POST", path="/api/v1/join-requests/{id}/reject"
    ),
    ActionKey.PLACEMENT_ACCEPT: ActionRoute(
        method="POST", path="/api/v1/projects/{id}/placement/accept"
    ),
    ActionKey.PLACEMENT_DECLINE: ActionRoute(
        method="POST", path="/api/v1/projects/{id}/placement/decline", body={"withdraw": False}
    ),
    ActionKey.EVENT_ENROLL: ActionRoute(method="POST", path="/api/v1/events/{id}/enroll", body={}),
    ActionKey.HELP_OFFER_ACCEPT: ActionRoute(method="POST", path="/api/v1/help-offers/{id}/accept"),
    ActionKey.HELP_REQUEST_DECLINE: ActionRoute(
        method="POST", path="/api/v1/help-requests/{id}/decline"
    ),
    ActionKey.HELP_REQUEST_OPEN: ActionRoute(method="POST", path="/api/v1/help-requests/{id}/open"),
    ActionKey.MENTORSHIP_ACKNOWLEDGE: ActionRoute(
        method="POST", path="/api/v1/mentorships/{id}/acknowledge", body={}
    ),
}


ALLOWED_ACTIONS: dict[NotificationType, tuple[ActionKey, ...]] = {
    NotificationType.PARTY_INVITE: (ActionKey.INVITE_ACCEPT, ActionKey.INVITE_DECLINE),
    NotificationType.TEAM_INVITE: (ActionKey.INVITE_ACCEPT, ActionKey.INVITE_DECLINE),
    NotificationType.INVITE_ACCEPTED: (),
    NotificationType.INVITE_DECLINED: (),
    NotificationType.INVITE_EXPIRED: (),
    NotificationType.INVITE_CLOSED: (),
    NotificationType.JOIN_REQUEST_ACCEPTED: (),
    NotificationType.JOIN_REQUEST_REJECTED: (),
    NotificationType.FORMATION_SOON: (),
    NotificationType.TEAM_PROPOSED: (ActionKey.PLACEMENT_ACCEPT, ActionKey.PLACEMENT_DECLINE),
    NotificationType.RESERVE_PLACED: (),
    NotificationType.ACCEPT_REMINDER: (ActionKey.PLACEMENT_ACCEPT, ActionKey.PLACEMENT_DECLINE),
    NotificationType.PLACEMENT_LAPSED: (ActionKey.EVENT_ENROLL,),
    NotificationType.TEAM_MEMBER_JOINED: (),
    NotificationType.TEAM_MEMBER_LEFT: (),
    NotificationType.LEAD_ASSIGNED: (),
    NotificationType.TEAM_DISBANDED: (),
    NotificationType.TEAM_STATUS_CHANGED: (),
    NotificationType.EVENT_CHANGED: (),
    NotificationType.EVENT_CANCELLED: (),
    NotificationType.ATTENDANCE_CONFIRM_DIGEST: (),
    NotificationType.PROJECT_INVITE_RECEIVED: (ActionKey.INVITE_ACCEPT, ActionKey.INVITE_DECLINE),
    NotificationType.PROJECT_JOIN_REQUEST_RECEIVED: (
        ActionKey.JOIN_REQUEST_ACCEPT,
        ActionKey.JOIN_REQUEST_REJECT,
    ),
    NotificationType.PROJECT_MEMBER_JOINED: (),
    NotificationType.PROJECT_MEMBER_LEFT: (),
    NotificationType.PROJECT_MEMBER_REMOVED: (),
    NotificationType.PROJECT_IDLE: (),
    NotificationType.PROJECT_PAUSED: (),
    NotificationType.PROJECT_RELEASED: (),
    NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST: (),
    NotificationType.HELP_REQUEST_MATCH: (),
    NotificationType.HELP_REQUEST_DIRECT: (ActionKey.HELP_REQUEST_DECLINE,),
    NotificationType.HELP_REQUEST_DECLINED: (ActionKey.HELP_REQUEST_OPEN,),
    NotificationType.HELP_OFFER_RECEIVED: (ActionKey.HELP_OFFER_ACCEPT,),
    NotificationType.HELP_OFFER_ACCEPTED: (),
    NotificationType.HELP_OFFER_CLOSED: (),
    NotificationType.HELP_REQUEST_EXPIRED: (),
    NotificationType.MENTORSHIP_STARTED: (),
    NotificationType.MENTORSHIP_IDLE: (),
    NotificationType.MENTORSHIP_STALLED: (),
    NotificationType.MENTORSHIP_CLOSE_PROPOSED: (),
    NotificationType.MENTORSHIP_CLOSED: (ActionKey.MENTORSHIP_ACKNOWLEDGE,),
    NotificationType.PENALTY_APPLIED: (),
    NotificationType.RESTRICTION_LIFTED: (),
    NotificationType.RESULT_DUE: (),
    NotificationType.REVIEWS_OPEN: (),
    NotificationType.REVIEWS_PENDING: (),
    NotificationType.REVIEW_RECEIVED: (),
    NotificationType.REVIEW_REMOVED: (),
    NotificationType.SCORE_CHANGED: (),
    NotificationType.SCORES_REVIEWED: (),
    NotificationType.ONBOARDING_REOPENED: (),
    NotificationType.TELEGRAM_LINK_BROKEN: (),
    NotificationType.ADMIN_ALERT: (),
    NotificationType.ADMIN_DIGEST: (),
}

TELEGRAM_OFF_BY_DEFAULT: frozenset[NotificationType] = frozenset(
    {
        NotificationType.SCORE_CHANGED,
        NotificationType.TEAM_MEMBER_JOINED,
        NotificationType.PROJECT_MEMBER_JOINED,
        NotificationType.PROJECT_RELEASED,
        NotificationType.ATTENDANCE_CONFIRM_DIGEST,
    }
)


class AlertKind(StrEnum):
    FORMATION_FAILED = "formation_failed"
    TEAM_BELOW_MIN = "team_below_min"
    EVENT_DATES_CHANGED = "event_dates_changed"
    RESULT_OVERDUE = "result_overdue"
    IMPORT_REVIEW = "import_review"
    EVENT_CANCELLED_WITH_TEAMS = "event_cancelled_with_teams"
    TEAM_SIZE_CONFLICT = "team_size_conflict"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    REVIEW_REPORTED = "review_reported"
    BANNED_MEMBER_IN_ACTIVE_TEAM = "banned_member_in_active_team"
    ALGO_FAILURE = "algo_failure"
    JOB_FAILING = "job_failing"
    PARSER_DOWN = "parser_down"


ALERT_SEVERITIES: dict[AlertKind, AlertSeverity] = {
    AlertKind.FORMATION_FAILED: AlertSeverity.WARNING,
    AlertKind.TEAM_BELOW_MIN: AlertSeverity.INFO,
    AlertKind.EVENT_DATES_CHANGED: AlertSeverity.INFO,
    AlertKind.RESULT_OVERDUE: AlertSeverity.WARNING,
    AlertKind.IMPORT_REVIEW: AlertSeverity.INFO,
    AlertKind.EVENT_CANCELLED_WITH_TEAMS: AlertSeverity.WARNING,
    AlertKind.TEAM_SIZE_CONFLICT: AlertSeverity.WARNING,
    AlertKind.SUSPICIOUS_ACTIVITY: AlertSeverity.WARNING,
    AlertKind.REVIEW_REPORTED: AlertSeverity.INFO,
    AlertKind.BANNED_MEMBER_IN_ACTIVE_TEAM: AlertSeverity.WARNING,
    AlertKind.ALGO_FAILURE: AlertSeverity.CRITICAL,
    AlertKind.JOB_FAILING: AlertSeverity.CRITICAL,
    AlertKind.PARSER_DOWN: AlertSeverity.CRITICAL,
}


class ApiAction(Contract):
    kind: Literal["api"] = "api"
    key: ActionKey
    entity_id: SubjectId
    label_key: str = Field(min_length=1)


class LinkAction(Contract):
    kind: Literal["link"] = "link"
    label_key: str = Field(min_length=1)
    url: WebPath


class NotificationPerson(Contract):
    user_id: UserId
    display_name: str


class AttendanceConfirmationCandidate(NotificationPerson):
    attendance_id: AttendanceId


class ScoreChange(Contract):
    area_key: AreaKey
    area_name: str
    previous_score: float = Field(ge=0, le=10)
    score: float = Field(ge=0, le=10)
    reason: str


class NotificationChange(Contract):
    field: str
    previous: str | None
    current: str | None


class AdminAlert(Contract):
    id: AdminAlertId
    type: AlertKind
    severity: AlertSeverity
    subject_type: str
    subject_id: SubjectId | None
    subject_title: str
    title: str
    message: str
    link: WebPath
    status: AlertStatus = AlertStatus.OPEN
    created_at: AwareDatetime
    resolved_by: UserId | None = None
    resolved_at: AwareDatetime | None = None
    resolution_note: str | None = None


class _Payload(Contract):
    type: NotificationType
    link: WebPath
    actions: tuple[Annotated[ApiAction | LinkAction, Field(discriminator="kind")], ...] = ()

    @model_validator(mode="after")
    def validate_actions(self) -> Self:
        if any(
            isinstance(action, ApiAction) and action.key not in ALLOWED_ACTIONS[self.type]
            for action in self.actions
        ):
            raise ValueError("Action is not allowed for this notification type")
        return self


class _EventPayload(_Payload):
    event_id: EventId
    event_title: str


class _TeamPayload(_EventPayload):
    project_id: ProjectId
    team_name: str


class _ProjectPayload(_Payload):
    project_id: ProjectId
    project_name: str


class _TeamInvitePayload(_TeamPayload):
    invite_id: InviteId
    kind: Literal[InviteKind.TEAM] = InviteKind.TEAM
    actor: NotificationPerson
    invitee: NotificationPerson
    expires_at: AwareDatetime
    message: str | None = None
    area_key: AreaKey | None = None


class TeamInvitePayload(_TeamInvitePayload):
    type: Literal[NotificationType.TEAM_INVITE] = NotificationType.TEAM_INVITE


class PartyInvitePayload(_EventPayload):
    type: Literal[NotificationType.PARTY_INVITE] = NotificationType.PARTY_INVITE
    party_id: PartyId
    invite_id: InviteId
    kind: Literal[InviteKind.PARTY] = InviteKind.PARTY
    actor: NotificationPerson
    invitee: NotificationPerson
    expires_at: AwareDatetime


class _InviteOutcomePayload(_Payload):
    invite_id: InviteId
    kind: InviteKind
    party_id: PartyId | None = None
    project_id: ProjectId | None = None
    project_name: str | None = None
    actor: NotificationPerson
    invitee: NotificationPerson
    expires_at: AwareDatetime
    event_id: EventId | None = None
    event_title: str | None = None


class InviteAcceptedPayload(_InviteOutcomePayload):
    type: Literal[NotificationType.INVITE_ACCEPTED] = NotificationType.INVITE_ACCEPTED


class InviteDeclinedPayload(_InviteOutcomePayload):
    type: Literal[NotificationType.INVITE_DECLINED] = NotificationType.INVITE_DECLINED


class InviteExpiredPayload(_InviteOutcomePayload):
    type: Literal[NotificationType.INVITE_EXPIRED] = NotificationType.INVITE_EXPIRED


class InviteClosedPayload(_InviteOutcomePayload):
    type: Literal[NotificationType.INVITE_CLOSED] = NotificationType.INVITE_CLOSED
    reason: str


class _JoinRequestOutcomePayload(_ProjectPayload):
    join_request_id: JoinRequestId
    requester: NotificationPerson
    lead: NotificationPerson
    event_id: EventId | None = None
    event_title: str | None = None


class JoinRequestAcceptedPayload(_JoinRequestOutcomePayload):
    type: Literal[NotificationType.JOIN_REQUEST_ACCEPTED] = NotificationType.JOIN_REQUEST_ACCEPTED


class JoinRequestRejectedPayload(_JoinRequestOutcomePayload):
    type: Literal[NotificationType.JOIN_REQUEST_REJECTED] = NotificationType.JOIN_REQUEST_REJECTED


class FormationSoonPayload(_EventPayload):
    type: Literal[NotificationType.FORMATION_SOON] = NotificationType.FORMATION_SOON
    formation_at: AwareDatetime
    timezone: TimeZoneName
    enrolled_count: int = Field(ge=0)
    team_count: int = Field(ge=0)


class _PlacementPayload(_TeamPayload):
    project_member_id: ProjectMemberId
    accept_by: AwareDatetime
    timezone: TimeZoneName
    members: tuple[NotificationPerson, ...]


class TeamProposedPayload(_PlacementPayload):
    type: Literal[NotificationType.TEAM_PROPOSED] = NotificationType.TEAM_PROPOSED
    proposal_reason: str
    area_key: AreaKey | None = None


class ReservePlacedPayload(_EventPayload):
    type: Literal[NotificationType.RESERVE_PLACED] = NotificationType.RESERVE_PLACED
    reserve_count: int = Field(ge=1)


class AcceptReminderPayload(_PlacementPayload):
    type: Literal[NotificationType.ACCEPT_REMINDER] = NotificationType.ACCEPT_REMINDER
    hours_remaining: int = Field(ge=0)


class PlacementLapsedPayload(_TeamPayload):
    type: Literal[NotificationType.PLACEMENT_LAPSED] = NotificationType.PLACEMENT_LAPSED
    accept_by: AwareDatetime
    reason: str
    reg_deadline: AwareDatetime


class TeamMemberJoinedPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_MEMBER_JOINED] = NotificationType.TEAM_MEMBER_JOINED
    member: NotificationPerson
    member_count: int = Field(ge=1)


class TeamMemberLeftPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_MEMBER_LEFT] = NotificationType.TEAM_MEMBER_LEFT
    member: NotificationPerson
    member_count: int = Field(ge=0)
    reason: str


class LeadAssignedPayload(_ProjectPayload):
    type: Literal[NotificationType.LEAD_ASSIGNED] = NotificationType.LEAD_ASSIGNED
    team_name: str
    event_id: EventId | None = None
    event_title: str | None = None
    lead: NotificationPerson
    reason: str


class TeamDisbandedPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_DISBANDED] = NotificationType.TEAM_DISBANDED
    reason: str


class TeamStatusChangedPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_STATUS_CHANGED] = NotificationType.TEAM_STATUS_CHANGED
    previous_status: TeamStatus
    status: TeamStatus
    reason: str


class EventChangedPayload(_EventPayload):
    type: Literal[NotificationType.EVENT_CHANGED] = NotificationType.EVENT_CHANGED
    changes: tuple[NotificationChange, ...]
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    timezone: TimeZoneName


class EventCancelledPayload(_EventPayload):
    type: Literal[NotificationType.EVENT_CANCELLED] = NotificationType.EVENT_CANCELLED
    reason: str


class AttendanceConfirmDigestPayload(_EventPayload):
    type: Literal[NotificationType.ATTENDANCE_CONFIRM_DIGEST] = (
        NotificationType.ATTENDANCE_CONFIRM_DIGEST
    )
    attendees: tuple[AttendanceConfirmationCandidate, ...]
    attendee_count: int = Field(ge=0)
    ends_at: AwareDatetime


class ProjectInviteReceivedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_INVITE_RECEIVED] = (
        NotificationType.PROJECT_INVITE_RECEIVED
    )
    invite_id: InviteId
    kind: Literal[InviteKind.TEAM] = InviteKind.TEAM
    actor: NotificationPerson
    expires_at: AwareDatetime
    message: str | None = None
    area_key: AreaKey | None = None


class ProjectJoinRequestReceivedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_JOIN_REQUEST_RECEIVED] = (
        NotificationType.PROJECT_JOIN_REQUEST_RECEIVED
    )
    join_request_id: JoinRequestId
    requester: NotificationPerson
    note: str | None = None


class ProjectMemberJoinedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_MEMBER_JOINED] = NotificationType.PROJECT_MEMBER_JOINED
    member: NotificationPerson
    member_count: int = Field(ge=1)


class ProjectMemberLeftPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_MEMBER_LEFT] = NotificationType.PROJECT_MEMBER_LEFT
    member: NotificationPerson
    member_count: int = Field(ge=0)


class ProjectMemberRemovedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_MEMBER_REMOVED] = NotificationType.PROJECT_MEMBER_REMOVED
    member: NotificationPerson
    actor: NotificationPerson
    reason: str | None = None


class ProjectIdlePayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_IDLE] = NotificationType.PROJECT_IDLE
    idle_days: int = Field(ge=0)
    pauses_at: AwareDatetime


class ProjectPausedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_PAUSED] = NotificationType.PROJECT_PAUSED
    status: Literal[ProjectStatus.PAUSED] = ProjectStatus.PAUSED
    reason: str


class ProjectReleasedPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_RELEASED] = NotificationType.PROJECT_RELEASED
    release_id: ReleaseId
    release_title: str
    actor: NotificationPerson
    notes_md: str
    repo_url: str | None = None
    demo_url: str | None = None
    writeup_url: str | None = None


class ContributionsConfirmDigestPayload(_ProjectPayload):
    type: Literal[NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST] = (
        NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST
    )
    release_id: ReleaseId
    release_title: str
    lead: NotificationPerson
    project_member_id: ProjectMemberId


class _HelpRequestPayload(_Payload):
    help_request_id: HelpRequestId
    learner: NotificationPerson
    area_key: AreaKey
    area_name: str
    topic: str
    goal: str
    format: HelpRequestFormat
    expires_at: AwareDatetime


class HelpRequestMatchPayload(_HelpRequestPayload):
    type: Literal[NotificationType.HELP_REQUEST_MATCH] = NotificationType.HELP_REQUEST_MATCH
    reason: str


class HelpRequestDirectPayload(_HelpRequestPayload):
    type: Literal[NotificationType.HELP_REQUEST_DIRECT] = NotificationType.HELP_REQUEST_DIRECT
    respond_by: AwareDatetime


class HelpRequestDeclinedPayload(_HelpRequestPayload):
    type: Literal[NotificationType.HELP_REQUEST_DECLINED] = NotificationType.HELP_REQUEST_DECLINED


class _HelpOfferPayload(_HelpRequestPayload):
    help_offer_id: HelpOfferId
    mentor: NotificationPerson
    message: str


class HelpOfferReceivedPayload(_HelpOfferPayload):
    type: Literal[NotificationType.HELP_OFFER_RECEIVED] = NotificationType.HELP_OFFER_RECEIVED
    mentor_score: float = Field(ge=0, le=10)
    learner_score: float = Field(ge=0, le=10)
    mentor_rating: float | None = Field(default=None, ge=1, le=10)
    mentor_review_count: int = Field(ge=0)
    past_mentorship_count: int = Field(ge=0)
    kind: MentorshipKind


class HelpOfferAcceptedPayload(_HelpOfferPayload):
    type: Literal[NotificationType.HELP_OFFER_ACCEPTED] = NotificationType.HELP_OFFER_ACCEPTED
    mentorship_id: MentorshipId
    kind: MentorshipKind


class HelpOfferClosedPayload(_HelpOfferPayload):
    type: Literal[NotificationType.HELP_OFFER_CLOSED] = NotificationType.HELP_OFFER_CLOSED
    reason: str


class HelpRequestExpiredPayload(_HelpRequestPayload):
    type: Literal[NotificationType.HELP_REQUEST_EXPIRED] = NotificationType.HELP_REQUEST_EXPIRED


class _MentorshipPayload(_Payload):
    mentorship_id: MentorshipId
    help_request_id: HelpRequestId
    mentor: NotificationPerson
    learner: NotificationPerson
    area_key: AreaKey
    area_name: str
    topic: str
    goal: str
    kind: MentorshipKind


class MentorshipStartedPayload(_MentorshipPayload):
    type: Literal[NotificationType.MENTORSHIP_STARTED] = NotificationType.MENTORSHIP_STARTED
    started_at: AwareDatetime


class MentorshipIdlePayload(_MentorshipPayload):
    type: Literal[NotificationType.MENTORSHIP_IDLE] = NotificationType.MENTORSHIP_IDLE
    idle_days: int = Field(ge=0)
    stalls_at: AwareDatetime


class MentorshipStalledPayload(_MentorshipPayload):
    type: Literal[NotificationType.MENTORSHIP_STALLED] = NotificationType.MENTORSHIP_STALLED
    idle_days: int = Field(ge=0)
    closes_at: AwareDatetime


class MentorshipCloseProposedPayload(_MentorshipPayload):
    type: Literal[NotificationType.MENTORSHIP_CLOSE_PROPOSED] = (
        NotificationType.MENTORSHIP_CLOSE_PROPOSED
    )
    respond_by: AwareDatetime


class MentorshipClosedPayload(_MentorshipPayload):
    type: Literal[NotificationType.MENTORSHIP_CLOSED] = NotificationType.MENTORSHIP_CLOSED
    outcome: MentorshipOutcome | None
    summary: str | None = None
    closed_at: AwareDatetime
    review_window_id: ReviewWindowId
    review_closes_at: AwareDatetime


class PenaltyAppliedPayload(_Payload):
    type: Literal[NotificationType.PENALTY_APPLIED] = NotificationType.PENALTY_APPLIED
    user_id: UserId
    reason: str
    xp_delta: int = Field(le=0)
    restriction_id: RestrictionId | None = None
    restriction_kind: RestrictionKind | None = None
    expires_at: AwareDatetime | None = None


class RestrictionLiftedPayload(_Payload):
    type: Literal[NotificationType.RESTRICTION_LIFTED] = NotificationType.RESTRICTION_LIFTED
    restriction_id: RestrictionId
    restriction_kind: RestrictionKind
    reason: str


class ResultDuePayload(_TeamPayload):
    type: Literal[NotificationType.RESULT_DUE] = NotificationType.RESULT_DUE
    ends_at: AwareDatetime
    member_submit_at: AwareDatetime
    closes_at: AwareDatetime


class _ReviewsPayload(_Payload):
    review_window_id: ReviewWindowId
    subject_type: ReviewWindowSubjectType
    subject_id: SubjectId
    subject_title: str
    closes_at: AwareDatetime
    to_review: tuple[NotificationPerson, ...]
    submitted_count: int = Field(ge=0)


class ReviewsOpenPayload(_ReviewsPayload):
    type: Literal[NotificationType.REVIEWS_OPEN] = NotificationType.REVIEWS_OPEN


class ReviewsPendingPayload(_ReviewsPayload):
    type: Literal[NotificationType.REVIEWS_PENDING] = NotificationType.REVIEWS_PENDING


class ReviewReceivedPayload(_Payload):
    type: Literal[NotificationType.REVIEW_RECEIVED] = NotificationType.REVIEW_RECEIVED
    review_window_id: ReviewWindowId
    subject_id: SubjectId
    subject_title: str
    review_count: int = Field(ge=1)
    context: ReviewContext
    revealed_at: AwareDatetime


class ReviewRemovedPayload(_Payload):
    type: Literal[NotificationType.REVIEW_REMOVED] = NotificationType.REVIEW_REMOVED
    review_id: ReviewId
    reviewee: NotificationPerson
    subject_title: str
    reason: str


class ScoreChangedPayload(_Payload):
    type: Literal[NotificationType.SCORE_CHANGED] = NotificationType.SCORE_CHANGED
    changes: tuple[ScoreChange, ...]


class ScoresReviewedPayload(_Payload):
    type: Literal[NotificationType.SCORES_REVIEWED] = NotificationType.SCORES_REVIEWED
    reviewer: NotificationPerson
    changes: tuple[ScoreChange, ...]


class OnboardingReopenedPayload(_Payload):
    type: Literal[NotificationType.ONBOARDING_REOPENED] = NotificationType.ONBOARDING_REOPENED
    actor: NotificationPerson
    reason: str | None = None


class TelegramLinkBrokenPayload(_Payload):
    type: Literal[NotificationType.TELEGRAM_LINK_BROKEN] = NotificationType.TELEGRAM_LINK_BROKEN
    broken_at: AwareDatetime
    reason: str


class AdminAlertPayload(_Payload):
    type: Literal[NotificationType.ADMIN_ALERT] = NotificationType.ADMIN_ALERT
    alert: AdminAlert


class AdminDigestPayload(_Payload):
    type: Literal[NotificationType.ADMIN_DIGEST] = NotificationType.ADMIN_DIGEST
    period_starts_at: AwareDatetime
    period_ends_at: AwareDatetime
    pending_member_reviews: int = Field(ge=0)
    overdue_to_check_items: int = Field(ge=0)
    member_event_duplicates: int = Field(ge=0)
    results_to_verify: int = Field(ge=0)
    open_alerts: dict[AlertKind, Annotated[int, Field(ge=0)]]


type NotificationPayload = Annotated[
    PartyInvitePayload
    | TeamInvitePayload
    | InviteAcceptedPayload
    | InviteDeclinedPayload
    | InviteExpiredPayload
    | InviteClosedPayload
    | JoinRequestAcceptedPayload
    | JoinRequestRejectedPayload
    | FormationSoonPayload
    | TeamProposedPayload
    | ReservePlacedPayload
    | AcceptReminderPayload
    | PlacementLapsedPayload
    | TeamMemberJoinedPayload
    | TeamMemberLeftPayload
    | LeadAssignedPayload
    | TeamDisbandedPayload
    | TeamStatusChangedPayload
    | EventChangedPayload
    | EventCancelledPayload
    | AttendanceConfirmDigestPayload
    | ProjectInviteReceivedPayload
    | ProjectJoinRequestReceivedPayload
    | ProjectMemberJoinedPayload
    | ProjectMemberLeftPayload
    | ProjectMemberRemovedPayload
    | ProjectIdlePayload
    | ProjectPausedPayload
    | ProjectReleasedPayload
    | ContributionsConfirmDigestPayload
    | HelpRequestMatchPayload
    | HelpRequestDirectPayload
    | HelpRequestDeclinedPayload
    | HelpOfferReceivedPayload
    | HelpOfferAcceptedPayload
    | HelpOfferClosedPayload
    | HelpRequestExpiredPayload
    | MentorshipStartedPayload
    | MentorshipIdlePayload
    | MentorshipStalledPayload
    | MentorshipCloseProposedPayload
    | MentorshipClosedPayload
    | PenaltyAppliedPayload
    | RestrictionLiftedPayload
    | ResultDuePayload
    | ReviewsOpenPayload
    | ReviewsPendingPayload
    | ReviewReceivedPayload
    | ReviewRemovedPayload
    | ScoreChangedPayload
    | ScoresReviewedPayload
    | OnboardingReopenedPayload
    | TelegramLinkBrokenPayload
    | AdminAlertPayload
    | AdminDigestPayload,
    Field(discriminator="type"),
]


class Notification(Contract):
    id: NotificationId
    user_id: UserId
    type: NotificationType
    payload: NotificationPayload
    created_at: AwareDatetime
    read_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_type(self) -> Self:
        if self.type != self.payload.type:
            raise ValueError("Notification type must match payload type")
        return self


class PendingDeliveriesQuery(Contract):
    channel: NotificationChannel = NotificationChannel.TELEGRAM
    limit: int = Field(default=50, ge=1, le=100)
    wait: int = Field(default=25, ge=0, le=25)


class PendingDelivery(Contract):
    delivery_id: DeliveryId
    notification_id: NotificationId
    telegram_chat_id: int
    locale: Locale
    type: NotificationType
    payload: NotificationPayload
    created_at: AwareDatetime

    @property
    def actions(self) -> tuple[ApiAction | LinkAction, ...]:
        return self.payload.actions

    @model_validator(mode="after")
    def validate_type(self) -> Self:
        if self.type != self.payload.type:
            raise ValueError("Delivery type must match payload type")
        if self.type == NotificationType.TELEGRAM_LINK_BROKEN:
            raise ValueError("Telegram link broken notifications are web only")
        return self


class DeliveryAckResult(Contract):
    delivery_id: DeliveryId
    status: Literal[DeliveryStatus.SENT, DeliveryStatus.FAILED, "permanent_failure"]
    external_id: str | None = None
    error: str | None = None


class DeliveryAck(Contract):
    results: list[DeliveryAckResult]


class TelegramLinkRequest(Contract):
    code: str = Field(min_length=1)
    telegram_user_id: int
    chat_id: int
    username: str | None


class TelegramLinkResult(Contract):
    user_id: UserId
    display_name: str


class TelegramUnlinkRequest(Contract):
    telegram_user_id: int


class NotificationActionCallback(Contract):
    key: ActionKey
    entity_id: SubjectId

    def to_callback_data(self) -> str:
        data = f"a:{self.key}:{self.entity_id}"
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Telegram callback data exceeds 64 bytes")
        return data

    @classmethod
    def from_callback_data(cls, data: str) -> Self:
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Telegram callback data exceeds 64 bytes")
        prefix, separator, rest = data.partition(":")
        key, entity_separator, entity_id = rest.partition(":")
        if prefix != "a" or not separator or not entity_separator:
            raise ValueError("Invalid notification callback data")
        return cls.model_validate({"key": key, "entity_id": entity_id})


class NotificationActionResult(Contract):
    key: ActionKey
    entity_id: SubjectId
    success: bool
    message: str
    code: ErrorCode | None = None
