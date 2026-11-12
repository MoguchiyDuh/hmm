from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from hmm_contracts.common import (
    AdminAlertId,
    AlertSeverity,
    AlertStatus,
    AreaKey,
    AttendanceId,
    ConfirmKind,
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
    LeadOfferId,
    LeadOfferSource,
    Locale,
    MemberState,
    MentorshipId,
    MentorshipKind,
    MentorshipOutcome,
    MergeProposalId,
    MergeProposalStatus,
    NotificationChannel,
    NotificationId,
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
    SwapRequestId,
    SwapRequestStatus,
    TeamStatus,
    UserId,
)

__all__ = [
    "ALERT_SEVERITIES",
    "ALLOWED_ACTIONS",
    "TELEGRAM_OFF_BY_DEFAULT",
    "AdminAlert",
    "AdminAlertPayload",
    "AdminDigestPayload",
    "AlertKind",
    "AttendanceConfirmDigestPayload",
    "AttendanceConfirmationCandidate",
    "ConfirmReminderPayload",
    "ContributionsConfirmDigestPayload",
    "DeliveryAck",
    "DeliveryAckResult",
    "EventCancelledPayload",
    "EventChangedPayload",
    "FormationReadyPayload",
    "FormationRescheduledPayload",
    "FormationSoonPayload",
    "HelpOfferAcceptedPayload",
    "HelpOfferClosedPayload",
    "HelpOfferReceivedPayload",
    "HelpRequestDirectPayload",
    "HelpRequestExpiredPayload",
    "HelpRequestMatchPayload",
    "InviteAcceptedPayload",
    "InviteClosedPayload",
    "InviteDeclinedPayload",
    "InviteExpiredPayload",
    "InviteReceivedPayload",
    "JoinRequestAcceptedPayload",
    "JoinRequestReceivedPayload",
    "JoinRequestRejectedPayload",
    "LeadAssignedPayload",
    "LeadOfferDeclinedPayload",
    "LeadOfferPayload",
    "LeadOpportunityPayload",
    "LeadSteppedDownPayload",
    "LeadTransferredPayload",
    "MentorshipCloseProposedPayload",
    "MentorshipClosedPayload",
    "MentorshipIdlePayload",
    "MentorshipStalledPayload",
    "MentorshipStartedPayload",
    "MergeProposedPayload",
    "MergeResultPayload",
    "Notification",
    "NotificationAction",
    "NotificationActionCallback",
    "NotificationActionResult",
    "NotificationApi",
    "NotificationCandidate",
    "NotificationChange",
    "NotificationPayload",
    "NotificationPerson",
    "NotificationType",
    "OnboardingReopenedPayload",
    "PenaltyAppliedPayload",
    "PendingDeliveriesQuery",
    "PendingDelivery",
    "PlacementChangedPayload",
    "PlacementLapsedPayload",
    "PresenceCheckPayload",
    "ProjectIdlePayload",
    "ProjectInviteReceivedPayload",
    "ProjectJoinRequestReceivedPayload",
    "ProjectLeadClaimablePayload",
    "ProjectLeadOfferPayload",
    "ProjectMemberJoinedPayload",
    "ProjectMemberLeftPayload",
    "ProjectMemberRemovedPayload",
    "ProjectPausedPayload",
    "ProjectReleasedPayload",
    "ReserveOfferPayload",
    "ReservePlacedPayload",
    "RestrictionLiftedPayload",
    "ResultDuePayload",
    "ReviewEndingPayload",
    "ReviewReceivedPayload",
    "ReviewRemovedPayload",
    "ReviewsOpenPayload",
    "ReviewsPendingPayload",
    "ScoreChange",
    "ScoreChangedPayload",
    "ScoresReviewedPayload",
    "SeatAutofilledPayload",
    "SeatOpenedPayload",
    "SwapRequestReceivedPayload",
    "SwapResultPayload",
    "TeamDisbandedPayload",
    "TeamLockedPayload",
    "TeamMemberJoinedPayload",
    "TeamMemberLeftPayload",
    "TeamProposedPayload",
    "TeamReconfirmPayload",
    "TeamStatusChangedPayload",
    "TelegramLinkBrokenPayload",
    "TelegramLinkRequest",
    "TelegramLinkResult",
    "TelegramUnlinkRequest",
    "WebPath",
]

type WebPath = Annotated[str, StringConstraints(pattern=r"^/([^/\s][^\s]*)?$")]


class NotificationType(StrEnum):
    INVITE_RECEIVED = "invite_received"
    INVITE_ACCEPTED = "invite_accepted"
    INVITE_DECLINED = "invite_declined"
    INVITE_EXPIRED = "invite_expired"
    INVITE_CLOSED = "invite_closed"
    JOIN_REQUEST_RECEIVED = "join_request_received"
    JOIN_REQUEST_ACCEPTED = "join_request_accepted"
    JOIN_REQUEST_REJECTED = "join_request_rejected"
    FORMATION_SOON = "formation_soon"
    FORMATION_RESCHEDULED = "formation_rescheduled"
    TEAM_PROPOSED = "team_proposed"
    TEAM_RECONFIRM = "team_reconfirm"
    PRESENCE_CHECK = "presence_check"
    LEAD_OFFER = "lead_offer"
    LEAD_OFFER_DECLINED = "lead_offer_declined"
    FORMATION_READY = "formation_ready"
    RESERVE_PLACED = "reserve_placed"
    CONFIRM_REMINDER = "confirm_reminder"
    REVIEW_ENDING = "review_ending"
    PLACEMENT_LAPSED = "placement_lapsed"
    SWAP_REQUEST_RECEIVED = "swap_request_received"
    MERGE_PROPOSED = "merge_proposed"
    SWAP_RESULT = "swap_result"
    MERGE_RESULT = "merge_result"
    PLACEMENT_CHANGED = "placement_changed"
    TEAM_LOCKED = "team_locked"
    TEAM_MEMBER_JOINED = "team_member_joined"
    TEAM_MEMBER_LEFT = "team_member_left"
    SEAT_OPENED = "seat_opened"
    SEAT_AUTOFILLED = "seat_autofilled"
    RESERVE_OFFER = "reserve_offer"
    LEAD_OPPORTUNITY = "lead_opportunity"
    LEAD_TRANSFERRED = "lead_transferred"
    LEAD_STEPPED_DOWN = "lead_stepped_down"
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
    PROJECT_LEAD_OFFER = "project_lead_offer"
    PROJECT_LEAD_CLAIMABLE = "project_lead_claimable"
    HELP_REQUEST_MATCH = "help_request_match"
    HELP_REQUEST_DIRECT = "help_request_direct"
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


ALLOWED_ACTIONS: dict[NotificationType, tuple[str, ...]] = {
    NotificationType.INVITE_RECEIVED: ("accept", "decline"),
    NotificationType.INVITE_ACCEPTED: (),
    NotificationType.INVITE_DECLINED: (),
    NotificationType.INVITE_EXPIRED: (),
    NotificationType.INVITE_CLOSED: (),
    NotificationType.JOIN_REQUEST_RECEIVED: ("accept", "reject"),
    NotificationType.JOIN_REQUEST_ACCEPTED: (),
    NotificationType.JOIN_REQUEST_REJECTED: (),
    NotificationType.FORMATION_SOON: (),
    NotificationType.FORMATION_RESCHEDULED: (),
    NotificationType.TEAM_PROPOSED: ("confirm", "decline"),
    NotificationType.TEAM_RECONFIRM: ("confirm", "leave"),
    NotificationType.PRESENCE_CHECK: ("confirm", "leave"),
    NotificationType.LEAD_OFFER: ("accept", "decline"),
    NotificationType.LEAD_OFFER_DECLINED: (),
    NotificationType.FORMATION_READY: ("confirm", "open_team"),
    NotificationType.RESERVE_PLACED: (),
    NotificationType.CONFIRM_REMINDER: ("confirm", "decline"),
    NotificationType.REVIEW_ENDING: ("open_team",),
    NotificationType.PLACEMENT_LAPSED: ("enroll_again",),
    NotificationType.SWAP_REQUEST_RECEIVED: ("accept", "decline"),
    NotificationType.MERGE_PROPOSED: ("accept", "decline"),
    NotificationType.SWAP_RESULT: (),
    NotificationType.MERGE_RESULT: (),
    NotificationType.PLACEMENT_CHANGED: (),
    NotificationType.TEAM_LOCKED: (),
    NotificationType.TEAM_MEMBER_JOINED: (),
    NotificationType.TEAM_MEMBER_LEFT: (),
    NotificationType.SEAT_OPENED: ("open_team",),
    NotificationType.SEAT_AUTOFILLED: (),
    NotificationType.RESERVE_OFFER: ("confirm", "decline"),
    NotificationType.LEAD_OPPORTUNITY: ("claim_lead",),
    NotificationType.LEAD_TRANSFERRED: (),
    NotificationType.LEAD_STEPPED_DOWN: (),
    NotificationType.LEAD_ASSIGNED: ("transfer", "step_down"),
    NotificationType.TEAM_DISBANDED: (),
    NotificationType.TEAM_STATUS_CHANGED: (),
    NotificationType.EVENT_CHANGED: (),
    NotificationType.EVENT_CANCELLED: (),
    NotificationType.ATTENDANCE_CONFIRM_DIGEST: ("confirm_attendees",),
    NotificationType.PROJECT_INVITE_RECEIVED: ("accept", "decline"),
    NotificationType.PROJECT_JOIN_REQUEST_RECEIVED: ("accept", "reject"),
    NotificationType.PROJECT_MEMBER_JOINED: (),
    NotificationType.PROJECT_MEMBER_LEFT: (),
    NotificationType.PROJECT_MEMBER_REMOVED: (),
    NotificationType.PROJECT_IDLE: ("open_project",),
    NotificationType.PROJECT_PAUSED: ("open_project",),
    NotificationType.PROJECT_RELEASED: (),
    NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST: ("confirm_lead",),
    NotificationType.PROJECT_LEAD_OFFER: ("accept",),
    NotificationType.PROJECT_LEAD_CLAIMABLE: ("claim",),
    NotificationType.HELP_REQUEST_MATCH: ("help",),
    NotificationType.HELP_REQUEST_DIRECT: ("help", "decline"),
    NotificationType.HELP_OFFER_RECEIVED: ("accept",),
    NotificationType.HELP_OFFER_ACCEPTED: (),
    NotificationType.HELP_OFFER_CLOSED: (),
    NotificationType.HELP_REQUEST_EXPIRED: ("reopen",),
    NotificationType.MENTORSHIP_STARTED: (),
    NotificationType.MENTORSHIP_IDLE: ("check_in",),
    NotificationType.MENTORSHIP_STALLED: ("check_in",),
    NotificationType.MENTORSHIP_CLOSE_PROPOSED: ("close_with_outcome",),
    NotificationType.MENTORSHIP_CLOSED: ("acknowledge", "endorse"),
    NotificationType.PENALTY_APPLIED: (),
    NotificationType.RESTRICTION_LIFTED: (),
    NotificationType.RESULT_DUE: ("submit_result",),
    NotificationType.REVIEWS_OPEN: ("review",),
    NotificationType.REVIEWS_PENDING: ("review",),
    NotificationType.REVIEW_RECEIVED: (),
    NotificationType.REVIEW_REMOVED: (),
    NotificationType.SCORE_CHANGED: (),
    NotificationType.SCORES_REVIEWED: (),
    NotificationType.ONBOARDING_REOPENED: (),
    NotificationType.TELEGRAM_LINK_BROKEN: (),
    NotificationType.ADMIN_ALERT: ("open_alert",),
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
    FORMATION_UNDERSIZED = "formation_undersized"
    FORMATION_FAILED = "formation_failed"
    TEAM_BELOW_MIN = "team_below_min"
    EVENT_DATES_CHANGED = "event_dates_changed"
    FORCED_LEAD = "forced_lead"
    LEAD_BELOW_THRESHOLD = "lead_below_threshold"
    RESULT_OVERDUE = "result_overdue"
    IMPORT_REVIEW = "import_review"
    EVENT_CANCELLED_WITH_TEAMS = "event_cancelled_with_teams"
    TEAM_SIZE_CONFLICT = "team_size_conflict"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    RESTRICTION_VIOLATION = "restriction_violation"
    REVIEW_REPORTED = "review_reported"
    BANNED_MEMBER_IN_ACTIVE_TEAM = "banned_member_in_active_team"
    ALGO_FAILURE = "algo_failure"
    JOB_FAILING = "job_failing"
    PARSER_DOWN = "parser_down"


ALERT_SEVERITIES: dict[AlertKind, AlertSeverity] = {
    AlertKind.FORMATION_UNDERSIZED: AlertSeverity.INFO,
    AlertKind.FORMATION_FAILED: AlertSeverity.WARNING,
    AlertKind.TEAM_BELOW_MIN: AlertSeverity.WARNING,
    AlertKind.EVENT_DATES_CHANGED: AlertSeverity.INFO,
    AlertKind.FORCED_LEAD: AlertSeverity.INFO,
    AlertKind.LEAD_BELOW_THRESHOLD: AlertSeverity.INFO,
    AlertKind.RESULT_OVERDUE: AlertSeverity.WARNING,
    AlertKind.IMPORT_REVIEW: AlertSeverity.INFO,
    AlertKind.EVENT_CANCELLED_WITH_TEAMS: AlertSeverity.WARNING,
    AlertKind.TEAM_SIZE_CONFLICT: AlertSeverity.WARNING,
    AlertKind.SUSPICIOUS_ACTIVITY: AlertSeverity.WARNING,
    AlertKind.RESTRICTION_VIOLATION: AlertSeverity.INFO,
    AlertKind.REVIEW_REPORTED: AlertSeverity.INFO,
    AlertKind.BANNED_MEMBER_IN_ACTIVE_TEAM: AlertSeverity.WARNING,
    AlertKind.ALGO_FAILURE: AlertSeverity.CRITICAL,
    AlertKind.JOB_FAILING: AlertSeverity.CRITICAL,
    AlertKind.PARSER_DOWN: AlertSeverity.CRITICAL,
}


class NotificationApi(Contract):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: WebPath


class NotificationAction(Contract):
    action: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,23}$")]
    entity_id: SubjectId
    label_key: str = Field(min_length=1)
    api: NotificationApi


class NotificationPerson(Contract):
    user_id: UserId
    display_name: str


class NotificationCandidate(NotificationPerson):
    rank: int = Field(ge=1)
    area_key: AreaKey | None = None
    reason: str


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
    actions: tuple[NotificationAction, ...] = ()

    @model_validator(mode="after")
    def validate_actions(self) -> Self:
        if any(action.action not in ALLOWED_ACTIONS[self.type] for action in self.actions):
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
    kind: Literal[InviteKind.CORE, InviteKind.FILL]
    actor: NotificationPerson
    invitee: NotificationPerson
    expires_at: AwareDatetime
    message: str | None = None
    area_key: AreaKey | None = None


class InviteReceivedPayload(_TeamInvitePayload):
    type: Literal[NotificationType.INVITE_RECEIVED] = NotificationType.INVITE_RECEIVED


class _InviteOutcomePayload(_ProjectPayload):
    invite_id: InviteId
    kind: InviteKind
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


class _TeamJoinRequestPayload(_TeamPayload):
    join_request_id: JoinRequestId
    requester: NotificationPerson
    lead: NotificationPerson
    note: str | None = None


class JoinRequestReceivedPayload(_TeamJoinRequestPayload):
    type: Literal[NotificationType.JOIN_REQUEST_RECEIVED] = NotificationType.JOIN_REQUEST_RECEIVED


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
    timezone: str
    enrolled_count: int = Field(ge=0)
    team_count: int = Field(ge=0)


class FormationRescheduledPayload(_EventPayload):
    type: Literal[NotificationType.FORMATION_RESCHEDULED] = NotificationType.FORMATION_RESCHEDULED
    previous_formation_at: AwareDatetime
    formation_at: AwareDatetime
    timezone: str
    reason: str


class _PlacementPayload(_TeamPayload):
    project_member_id: ProjectMemberId
    confirm_by: AwareDatetime
    timezone: str
    members: tuple[NotificationPerson, ...]


class TeamProposedPayload(_PlacementPayload):
    type: Literal[NotificationType.TEAM_PROPOSED] = NotificationType.TEAM_PROPOSED
    proposal_reason: str
    area_key: AreaKey | None = None


class TeamReconfirmPayload(_PlacementPayload):
    type: Literal[NotificationType.TEAM_RECONFIRM] = NotificationType.TEAM_RECONFIRM


class PresenceCheckPayload(_PlacementPayload):
    type: Literal[NotificationType.PRESENCE_CHECK] = NotificationType.PRESENCE_CHECK
    starts_at: AwareDatetime


class LeadOfferPayload(_TeamPayload):
    type: Literal[NotificationType.LEAD_OFFER] = NotificationType.LEAD_OFFER
    lead_offer_id: LeadOfferId
    source: LeadOfferSource
    offered_member: NotificationPerson
    offered_by: NotificationPerson | None = None
    expires_at: AwareDatetime


class LeadOfferDeclinedPayload(_TeamPayload):
    type: Literal[NotificationType.LEAD_OFFER_DECLINED] = NotificationType.LEAD_OFFER_DECLINED
    lead_offer_id: LeadOfferId
    offered_member: NotificationPerson
    reason: str


class FormationReadyPayload(_PlacementPayload):
    type: Literal[NotificationType.FORMATION_READY] = NotificationType.FORMATION_READY
    proposed_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)


class ReservePlacedPayload(_EventPayload):
    type: Literal[NotificationType.RESERVE_PLACED] = NotificationType.RESERVE_PLACED
    rank: int = Field(ge=1)
    reserve_count: int = Field(ge=1)


class ConfirmReminderPayload(_PlacementPayload):
    type: Literal[NotificationType.CONFIRM_REMINDER] = NotificationType.CONFIRM_REMINDER
    confirm_kind: ConfirmKind
    hours_remaining: int = Field(ge=0)


class ReviewEndingPayload(_TeamPayload):
    type: Literal[NotificationType.REVIEW_ENDING] = NotificationType.REVIEW_ENDING
    review_ends_at: AwareDatetime
    timezone: str
    member_count: int = Field(ge=0)
    viable_size: int = Field(ge=1)
    has_lead: bool


class PlacementLapsedPayload(_TeamPayload):
    type: Literal[NotificationType.PLACEMENT_LAPSED] = NotificationType.PLACEMENT_LAPSED
    confirm_by: AwareDatetime
    reason: str
    reg_deadline: AwareDatetime


class _SwapPayload(_TeamPayload):
    swap_request_id: SwapRequestId
    other_project_id: ProjectId
    other_team_name: str
    from_member: NotificationPerson
    to_member: NotificationPerson


class SwapRequestReceivedPayload(_SwapPayload):
    type: Literal[NotificationType.SWAP_REQUEST_RECEIVED] = NotificationType.SWAP_REQUEST_RECEIVED
    expires_at: AwareDatetime


class SwapResultPayload(_SwapPayload):
    type: Literal[NotificationType.SWAP_RESULT] = NotificationType.SWAP_RESULT
    status: SwapRequestStatus
    reason: str


class _MergePayload(_TeamPayload):
    merge_proposal_id: MergeProposalId
    target_project_id: ProjectId
    target_team_name: str
    target_lead: NotificationPerson
    members: tuple[NotificationPerson, ...]
    merged_size: int = Field(ge=1)


class MergeProposedPayload(_MergePayload):
    type: Literal[NotificationType.MERGE_PROPOSED] = NotificationType.MERGE_PROPOSED
    expires_at: AwareDatetime
    reason: str


class MergeResultPayload(_MergePayload):
    type: Literal[NotificationType.MERGE_RESULT] = NotificationType.MERGE_RESULT
    status: MergeProposalStatus
    reason: str


class PlacementChangedPayload(_TeamPayload):
    type: Literal[NotificationType.PLACEMENT_CHANGED] = NotificationType.PLACEMENT_CHANGED
    project_member_id: ProjectMemberId
    state: MemberState
    confirm_by: AwareDatetime | None = None
    confirm_kind: ConfirmKind | None = None
    timezone: str
    members: tuple[NotificationPerson, ...]
    previous_project_id: ProjectId
    previous_team_name: str
    reason: str


class TeamLockedPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_LOCKED] = NotificationType.TEAM_LOCKED
    members: tuple[NotificationPerson, ...]
    lead: NotificationPerson
    starts_at: AwareDatetime
    timezone: str
    reserve_count: int = Field(ge=0)


class TeamMemberJoinedPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_MEMBER_JOINED] = NotificationType.TEAM_MEMBER_JOINED
    member: NotificationPerson
    member_count: int = Field(ge=1)


class TeamMemberLeftPayload(_TeamPayload):
    type: Literal[NotificationType.TEAM_MEMBER_LEFT] = NotificationType.TEAM_MEMBER_LEFT
    member: NotificationPerson
    member_count: int = Field(ge=0)
    reason: str


class SeatOpenedPayload(_TeamPayload):
    type: Literal[NotificationType.SEAT_OPENED] = NotificationType.SEAT_OPENED
    open_seats: int = Field(ge=1)
    head_start_ends_at: AwareDatetime
    reg_deadline: AwareDatetime
    quick_pick: tuple[NotificationCandidate, ...]


class SeatAutofilledPayload(_TeamPayload):
    type: Literal[NotificationType.SEAT_AUTOFILLED] = NotificationType.SEAT_AUTOFILLED
    member: NotificationPerson
    open_seats: int = Field(ge=0)


class ReserveOfferPayload(_PlacementPayload):
    type: Literal[NotificationType.RESERVE_OFFER] = NotificationType.RESERVE_OFFER
    rank: int = Field(ge=1)
    proposal_reason: str


class LeadOpportunityPayload(_EventPayload):
    type: Literal[NotificationType.LEAD_OPPORTUNITY] = NotificationType.LEAD_OPPORTUNITY
    expires_at: AwareDatetime
    reserve_count: int = Field(ge=0)
    viable_size: int = Field(ge=1)


class LeadTransferredPayload(_TeamPayload):
    type: Literal[NotificationType.LEAD_TRANSFERRED] = NotificationType.LEAD_TRANSFERRED
    previous_lead: NotificationPerson
    lead: NotificationPerson


class LeadSteppedDownPayload(_TeamPayload):
    type: Literal[NotificationType.LEAD_STEPPED_DOWN] = NotificationType.LEAD_STEPPED_DOWN
    previous_lead: NotificationPerson


class LeadAssignedPayload(_TeamPayload):
    type: Literal[NotificationType.LEAD_ASSIGNED] = NotificationType.LEAD_ASSIGNED
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
    timezone: str


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
    kind: Literal[InviteKind.PROJECT] = InviteKind.PROJECT
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


class ProjectLeadOfferPayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_LEAD_OFFER] = NotificationType.PROJECT_LEAD_OFFER
    lead_offer_id: LeadOfferId
    source: LeadOfferSource
    offered_by: NotificationPerson | None = None
    expires_at: AwareDatetime


class ProjectLeadClaimablePayload(_ProjectPayload):
    type: Literal[NotificationType.PROJECT_LEAD_CLAIMABLE] = NotificationType.PROJECT_LEAD_CLAIMABLE
    member_count: int = Field(ge=1)
    reason: str


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
    InviteReceivedPayload
    | InviteAcceptedPayload
    | InviteDeclinedPayload
    | InviteExpiredPayload
    | InviteClosedPayload
    | JoinRequestReceivedPayload
    | JoinRequestAcceptedPayload
    | JoinRequestRejectedPayload
    | FormationSoonPayload
    | FormationRescheduledPayload
    | TeamProposedPayload
    | TeamReconfirmPayload
    | PresenceCheckPayload
    | LeadOfferPayload
    | LeadOfferDeclinedPayload
    | FormationReadyPayload
    | ReservePlacedPayload
    | ConfirmReminderPayload
    | ReviewEndingPayload
    | PlacementLapsedPayload
    | SwapRequestReceivedPayload
    | MergeProposedPayload
    | SwapResultPayload
    | MergeResultPayload
    | PlacementChangedPayload
    | TeamLockedPayload
    | TeamMemberJoinedPayload
    | TeamMemberLeftPayload
    | SeatOpenedPayload
    | SeatAutofilledPayload
    | ReserveOfferPayload
    | LeadOpportunityPayload
    | LeadTransferredPayload
    | LeadSteppedDownPayload
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
    | ProjectLeadOfferPayload
    | ProjectLeadClaimablePayload
    | HelpRequestMatchPayload
    | HelpRequestDirectPayload
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
    def actions(self) -> tuple[NotificationAction, ...]:
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
    action: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,23}$")]
    entity_id: SubjectId

    def to_callback_data(self) -> str:
        return f"a:{self.action}:{self.entity_id}"

    @classmethod
    def from_callback_data(cls, data: str) -> Self:
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Telegram callback data exceeds 64 bytes")
        prefix, separator, rest = data.partition(":")
        action, entity_separator, entity_id = rest.partition(":")
        if prefix != "a" or not separator or not entity_separator:
            raise ValueError("Invalid notification callback data")
        return cls.model_validate({"action": action, "entity_id": entity_id})


class NotificationActionResult(Contract):
    action: str
    entity_id: SubjectId
    success: bool
    message: str
    code: ErrorCode | None = None
