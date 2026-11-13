from enum import StrEnum
from http import HTTPStatus
from types import UnionType
from typing import Annotated, Any, Self, get_args, get_origin
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "ERROR_STATUS",
    "SEED_AREA_KEYS",
    "ActivityEntryId",
    "ActivityType",
    "ActorKind",
    "AdminAlertId",
    "AlertSeverity",
    "AlertStatus",
    "AreaId",
    "AreaImportance",
    "AreaKey",
    "AreaScoreEventId",
    "AreaScoreEventKind",
    "AttendanceId",
    "AttendanceRole",
    "AttendanceStatus",
    "AuditAction",
    "AuditEntityType",
    "AuditLogId",
    "ConfirmKind",
    "ConfirmationFactType",
    "ConfirmationId",
    "ConfirmationSource",
    "Contract",
    "CvTextStatus",
    "DeliveryId",
    "DeliveryStatus",
    "EnrollmentId",
    "EnrollmentSource",
    "EnrollmentStatus",
    "ErrorCode",
    "EventFormat",
    "EventId",
    "EventKind",
    "EventOrigin",
    "EventPhase",
    "EventReviewReason",
    "EventRevisionId",
    "EventRevisionOrigin",
    "EventSourceId",
    "EventSourcePersonId",
    "EventStatus",
    "FactId",
    "FactType",
    "FeedItemId",
    "FlagId",
    "FlagStatus",
    "FlagSubjectType",
    "FormationKind",
    "FormationPhase",
    "FormationPlanner",
    "FormationRunId",
    "FormationRunStatus",
    "GithubSnapshotStatus",
    "HelpOfferId",
    "HelpOfferStatus",
    "HelpRequestFormat",
    "HelpRequestId",
    "HelpRequestStatus",
    "InviteId",
    "InviteKind",
    "InviteStatus",
    "JoinRequestId",
    "JoinRequestStatus",
    "JoinedVia",
    "LeadOfferId",
    "LeadOfferSource",
    "LeadOfferStatus",
    "LeftReason",
    "Locale",
    "MemberReviewStatus",
    "MemberRole",
    "MemberState",
    "MentorshipCheckinId",
    "MentorshipId",
    "MentorshipKind",
    "MentorshipOutcome",
    "MentorshipSide",
    "MentorshipStatus",
    "MergeProposalId",
    "MergeProposalStatus",
    "MergeProposedBy",
    "MilestoneId",
    "NotificationChannel",
    "NotificationId",
    "OnboardingStatus",
    "OnboardingSubmissionId",
    "OverrideEvidence",
    "Page",
    "ParserHeartbeatId",
    "PlaceStatus",
    "Problem",
    "ProjectId",
    "ProjectMemberId",
    "ProjectOrigin",
    "ProjectStatus",
    "ProjectUpdateId",
    "ProjectVisibility",
    "QuestionId",
    "QuestionOptionId",
    "QuizId",
    "QuizStatus",
    "RecommendationSetId",
    "RecommendationStatus",
    "RecommendationSubjectType",
    "ReleaseId",
    "RestrictionId",
    "RestrictionKind",
    "ResultOutcome",
    "ReviewContext",
    "ReviewId",
    "ReviewReportId",
    "ReviewReportStatus",
    "ReviewSubjectType",
    "ReviewWindowId",
    "ReviewWindowSubjectType",
    "Severity",
    "SourceId",
    "SourcePersonRole",
    "SubjectId",
    "SubmissionStatus",
    "SwapRequestId",
    "SwapRequestStatus",
    "TeamStatus",
    "TimeZoneName",
    "Trust",
    "UserId",
    "ValidationIssue",
    "XpAdjustmentId",
]

type UserId = UUID
type EventId = UUID
type ProjectId = UUID
type AreaId = UUID
type QuizId = UUID
type QuestionId = UUID
type QuestionOptionId = UUID
type OnboardingSubmissionId = UUID
type ActivityEntryId = UUID
type AreaScoreEventId = UUID
type XpAdjustmentId = UUID
type FlagId = UUID
type RestrictionId = UUID
type EventSourceId = UUID
type EventSourcePersonId = UUID
type EventRevisionId = UUID
type AttendanceId = UUID
type EnrollmentId = UUID
type ProjectMemberId = UUID
type InviteId = UUID
type JoinRequestId = UUID
type LeadOfferId = UUID
type SwapRequestId = UUID
type MergeProposalId = UUID
type FormationRunId = UUID
type ReleaseId = UUID
type MilestoneId = UUID
type ProjectUpdateId = UUID
type ReviewWindowId = UUID
type ReviewId = UUID
type ReviewReportId = UUID
type RecommendationSetId = UUID
type NotificationId = UUID
type DeliveryId = UUID
type AdminAlertId = UUID
type AuditLogId = UUID
type ParserHeartbeatId = UUID
type HelpRequestId = UUID
type HelpOfferId = UUID
type MentorshipId = UUID
type MentorshipCheckinId = UUID
type ConfirmationId = UUID
type FeedItemId = UUID
type FactId = UUID
type SubjectId = UUID
type SourceId = str
type AreaKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Timezone must be an IANA timezone") from error
    return value


type TimeZoneName = Annotated[str, Field(min_length=1), AfterValidator(_validate_timezone)]

SEED_AREA_KEYS: tuple[str, ...] = (
    "backend",
    "frontend",
    "mobile",
    "ml_ai",
    "data",
    "design",
    "product",
    "devops",
    "security",
    "gamedev",
    "hardware",
    "qa",
)


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Locale(StrEnum):
    EN = "en"
    RU = "ru"


class OnboardingStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REOPENED = "reopened"


class GithubSnapshotStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class CvTextStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"


class QuizStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class SubmissionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"


class OverrideEvidence(StrEnum):
    INTERVIEW = "interview"
    GITHUB_REVIEW = "github_review"
    CV_REVIEW = "cv_review"
    OTHER = "other"


class MemberReviewStatus(StrEnum):
    PENDING = "pending"
    REVIEWED = "reviewed"


class AreaScoreEventKind(StrEnum):
    CALCULATED = "calculated"
    OVERRIDE_SET = "override_set"
    OVERRIDE_REMOVED = "override_removed"


class ActivityType(StrEnum):
    LEARNING = "learning"
    COURSE = "course"
    TALK = "talk"
    WRITEUP = "writeup"
    OSS = "oss"
    OTHER = "other"


class Trust(StrEnum):
    SELF = "self"
    PEER = "peer"
    TRUSTED = "trusted"


class FactType(StrEnum):
    ATTENDANCE = "attendance"
    PROJECT_MEMBER = "project_member"
    MENTORSHIP_SIDE = "mentorship_side"
    ACTIVITY_ENTRY = "activity_entry"


class FlagSubjectType(StrEnum):
    ATTENDANCE = "attendance"
    PROJECT_MEMBER = "project_member"
    MENTORSHIP = "mentorship"
    CONFIRMATION = "confirmation"
    ACTIVITY_ENTRY = "activity_entry"
    REVIEW = "review"
    PROFILE = "profile"
    RING = "ring"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FlagStatus(StrEnum):
    TO_CHECK = "to_check"
    PARDONED = "pardoned"
    PUNISHED = "punished"


class RestrictionKind(StrEnum):
    CONFIRMING = "confirming"
    MENTORING = "mentoring"
    CREATING_EVENTS = "creating_events"
    CREATING_PROJECTS = "creating_projects"


class EventKind(StrEnum):
    HACKATHON = "hackathon"
    CONTEST = "contest"
    CONFERENCE = "conference"
    MEETUP = "meetup"
    WORKSHOP = "workshop"
    COURSE = "course"
    OTHER = "other"


class EventFormat(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    HYBRID = "hybrid"


class EventStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class EventReviewReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    UNMAPPED_AREAS = "unmapped_areas"
    MANUAL = "manual"


class EventOrigin(StrEnum):
    IMPORT = "import"
    MANUAL = "manual"
    MEMBER = "member"


class AreaImportance(StrEnum):
    PRIMARY = "primary"
    REQUIRED = "required"
    NICE = "nice"


class SourcePersonRole(StrEnum):
    ATTENDEE = "attendee"
    SPEAKER = "speaker"
    ORGANIZER = "organizer"
    WINNER = "winner"


class EventRevisionOrigin(StrEnum):
    IMPORT = "import"
    ADMIN = "admin"
    MEMBER = "member"
    SYSTEM = "system"


class AttendanceStatus(StrEnum):
    GOING = "going"
    ATTENDED = "attended"
    CANCELLED = "cancelled"


class AttendanceRole(StrEnum):
    ATTENDEE = "attendee"
    SPEAKER = "speaker"
    ORGANIZER = "organizer"
    HOST = "host"
    VOLUNTEER = "volunteer"


class EventPhase(StrEnum):
    UPCOMING = "upcoming"
    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"
    ONGOING = "ongoing"
    ENDED = "ended"


class FormationPhase(StrEnum):
    OPEN = "open"
    FORMING = "forming"
    REVIEW = "review"
    LOCKED = "locked"
    CLOSED = "closed"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    PLACED = "placed"
    RESERVE = "reserve"
    WITHDRAWN = "withdrawn"
    LAPSED = "lapsed"
    EXPIRED = "expired"


class EnrollmentSource(StrEnum):
    SELF = "self"
    INVITE = "invite"


class ProjectVisibility(StrEnum):
    PUBLIC = "public"
    MEMBERS = "members"


class ProjectStatus(StrEnum):
    IDEA = "idea"
    ACTIVE = "active"
    SHIPPED = "shipped"
    PAUSED = "paused"
    ABANDONED = "abandoned"
    DISBANDED = "disbanded"
    CANCELLED = "cancelled"


class TeamStatus(StrEnum):
    FORMING = "forming"
    REVIEW = "review"
    LOCKED = "locked"
    ACTIVE = "active"
    FINISHED = "finished"
    WITHDRAWN = "withdrawn"
    DISBANDED = "disbanded"
    CANCELLED = "cancelled"


class ProjectOrigin(StrEnum):
    STANDALONE = "standalone"
    CLAIM = "claim"
    FORMATION = "formation"
    LATE_CLAIM = "late_claim"
    ADMIN = "admin"


class MemberRole(StrEnum):
    LEAD = "lead"
    MEMBER = "member"


class MemberState(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"


class JoinedVia(StrEnum):
    CREATE = "create"
    INVITE = "invite"
    CLAIM = "claim"
    CORE_INVITE = "core_invite"
    JOIN_REQUEST = "join_request"
    FORMATION = "formation"
    FILL_INVITE = "fill_invite"
    RESERVE_ADD = "reserve_add"
    RESERVE_OFFER = "reserve_offer"
    SWAP = "swap"
    MERGE = "merge"
    ADMIN = "admin"


class ConfirmKind(StrEnum):
    PLACEMENT = "placement"
    RECONFIRM = "reconfirm"
    PRESENCE = "presence"


class LeftReason(StrEnum):
    DECLINED = "declined"
    LAPSED = "lapsed"
    REMOVED = "removed"
    LEFT = "left"
    SWAPPED = "swapped"
    MERGED = "merged"
    DISBANDED = "disbanded"
    BANNED = "banned"
    ADMIN = "admin"


class InviteKind(StrEnum):
    CORE = "core"
    FILL = "fill"
    PROJECT = "project"


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    CLOSED = "closed"


class JoinRequestStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class LeadOfferSource(StrEnum):
    FORMATION = "formation"
    SUCCESSION = "succession"
    TRANSFER = "transfer"


class LeadOfferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class SwapRequestStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CLOSED = "closed"


class MergeProposedBy(StrEnum):
    SYSTEM = "system"
    LEAD = "lead"


class MergeProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CLOSED = "closed"


class FormationKind(StrEnum):
    MAIN = "main"
    LATE = "late"


class FormationRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    APPLIED = "applied"
    FAILED = "failed"


class FormationPlanner(StrEnum):
    ALGO = "algo"
    FALLBACK = "fallback"


class ResultOutcome(StrEnum):
    FINISHED = "finished"
    WITHDRAWN = "withdrawn"


class PlaceStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ReviewWindowSubjectType(StrEnum):
    PROJECT = "project"
    MENTORSHIP = "mentorship"


class ReviewSubjectType(StrEnum):
    PROJECT = "project"
    MENTORSHIP = "mentorship"
    EVENT = "event"


class ReviewContext(StrEnum):
    MEMBER = "member"
    LEAD = "lead"
    MENTOR = "mentor"
    HOST = "host"
    SPEAKER = "speaker"


class ReviewReportStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class RecommendationSubjectType(StrEnum):
    PROJECT = "project"
    HELP_REQUEST = "help_request"


class RecommendationStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ActorKind(StrEnum):
    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"
    SERVICE = "service"


class AuditAction(StrEnum):
    USER_EDIT = "user_edit"
    USER_BAN = "user_ban"
    USER_UNBAN = "user_unban"
    ADMIN_GRANT = "admin_grant"
    ADMIN_REVOKE = "admin_revoke"
    AREA_CREATE = "area_create"
    AREA_EDIT = "area_edit"
    AREA_ARCHIVE = "area_archive"
    AREA_UNARCHIVE = "area_unarchive"
    QUIZ_CREATE = "quiz_create"
    QUIZ_EDIT = "quiz_edit"
    QUIZ_CLONE = "quiz_clone"
    QUIZ_VALIDATE = "quiz_validate"
    QUIZ_PUBLISH = "quiz_publish"
    QUIZ_DELETE = "quiz_delete"
    ONBOARDING_REOPEN = "onboarding_reopen"
    MEMBER_REVIEW_EDIT = "member_review_edit"
    MEMBER_REVIEW_COMPLETE = "member_review_complete"
    SCORE_OVERRIDE_SET = "score_override_set"
    SCORE_OVERRIDE_REMOVE = "score_override_remove"
    XP_ADJUST = "xp_adjust"
    XP_ADJUST_REVERSE = "xp_adjust_reverse"
    MEMBER_RECOMPUTE = "member_recompute"
    FLAG_PARDON = "flag_pardon"
    FLAG_PUNISH = "flag_punish"
    FACT_EXCLUDE = "fact_exclude"
    CONFIRMATIONS_REVOKE = "confirmations_revoke"
    RESTRICTION_APPLY = "restriction_apply"
    RESTRICTION_LIFT = "restriction_lift"
    EVENT_CREATE = "event_create"
    EVENT_EDIT = "event_edit"
    EVENT_UNLOCK_FIELD = "event_unlock_field"
    EVENT_PUBLISH = "event_publish"
    EVENT_REJECT = "event_reject"
    EVENT_MERGE = "event_merge"
    EVENT_CANCEL = "event_cancel"
    FORMATION_EDIT = "formation_edit"
    FORMATION_RUN = "formation_run"
    RESERVE_REORDER = "reserve_reorder"
    PROJECT_CREATE = "project_create"
    PROJECT_DISBAND = "project_disband"
    PROJECT_STATUS_SET = "project_status_set"
    MEMBER_ADD = "member_add"
    MEMBER_REMOVE = "member_remove"
    MEMBER_MOVE = "member_move"
    LEAD_ASSIGN = "lead_assign"
    LEAD_TRANSFER = "lead_transfer"
    LEAD_STEP_DOWN = "lead_step_down"
    RESULT_SUBMIT = "result_submit"
    RESULT_EDIT = "result_edit"
    RESULT_VERIFY = "result_verify"
    RESULT_REJECT = "result_reject"
    REVIEW_REMOVE = "review_remove"
    ALERT_RESOLVE = "alert_resolve"
    SETTING_SET = "setting_set"
    JOB_RETRY = "job_retry"
    SYSTEM_TRANSITION = "system_transition"


class AuditEntityType(StrEnum):
    USER = "user"
    AREA = "area"
    QUIZ = "quiz"
    ONBOARDING = "onboarding"
    MEMBER_REVIEW = "member_review"
    AREA_SCORE = "area_score"
    XP_ADJUSTMENT = "xp_adjustment"
    FLAG = "flag"
    FACT = "fact"
    CONFIRMATION = "confirmation"
    RESTRICTION = "restriction"
    EVENT = "event"
    FORMATION = "formation"
    ENROLLMENT = "enrollment"
    PROJECT = "project"
    PROJECT_MEMBER = "project_member"
    INVITE = "invite"
    JOIN_REQUEST = "join_request"
    LEAD_OFFER = "lead_offer"
    SWAP_REQUEST = "swap_request"
    MERGE_PROPOSAL = "merge_proposal"
    PROJECT_RESULT = "project_result"
    REVIEW = "review"
    ADMIN_ALERT = "admin_alert"
    SETTING = "setting"
    JOB = "job"
    ATTENDANCE = "attendance"
    HELP_REQUEST = "help_request"
    HELP_OFFER = "help_offer"
    MENTORSHIP = "mentorship"


class HelpRequestFormat(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    ANY = "any"


class HelpRequestStatus(StrEnum):
    OPEN = "open"
    DIRECT = "direct"
    MATCHED = "matched"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class HelpOfferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class MentorshipKind(StrEnum):
    MENTORING = "mentoring"
    STUDY_PAIR = "study_pair"


class MentorshipSide(StrEnum):
    MENTOR = "mentor"
    LEARNER = "learner"


class MentorshipStatus(StrEnum):
    ACTIVE = "active"
    STALLED = "stalled"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class MentorshipOutcome(StrEnum):
    REACHED = "reached"
    PARTIAL = "partial"
    NOT_REACHED = "not_reached"


class ConfirmationFactType(StrEnum):
    ATTENDANCE = "attendance"
    PROJECT_MEMBER = "project_member"
    MENTORSHIP_SIDE = "mentorship_side"


class ConfirmationSource(StrEnum):
    PEER = "peer"
    LEAD = "lead"
    ORGANIZER = "organizer"
    HOST = "host"
    LEARNER = "learner"
    MENTOR = "mentor"
    SYSTEM = "system"
    ADMIN = "admin"


class ErrorCode(StrEnum):
    ALREADY_ENROLLED = "already_enrolled"
    ALREADY_IN_TEAM = "already_in_team"
    ALREADY_INVITED = "already_invited"
    ATTENDANCE_WINDOW_CLOSED = "attendance_window_closed"
    BAD_REQUEST = "bad_request"
    BANNED = "banned"
    CANNOT_CONFIRM_OWN_FACT = "cannot_confirm_own_fact"
    CHECKIN_LIMIT = "checkin_limit"
    CODE_EXPIRED = "code_expired"
    CODE_INVALID = "code_invalid"
    CONFIRMATION_WITHDRAW_CLOSED = "confirmation_withdraw_closed"
    CORE_FULL = "core_full"
    ENROLLMENT_CLOSED = "enrollment_closed"
    FILE_TOO_LARGE = "file_too_large"
    FORBIDDEN = "forbidden"
    FORMATION_IN_PROGRESS = "formation_in_progress"
    HELP_REQUEST_OPEN = "help_request_open"
    INTERNAL_ERROR = "internal_error"
    INVALID_STATE = "invalid_state"
    JOIN_REQUEST_PENDING = "join_request_pending"
    LAST_ADMIN = "last_admin"
    LEAD_FIT_PENDING = "lead_fit_pending"
    LEAD_FIT_TOO_LOW = "lead_fit_too_low"
    LEAD_NOT_SWAPPABLE = "lead_not_swappable"
    MENTEE_LIMIT_REACHED = "mentee_limit_reached"
    MENTOR_CAPACITY_REACHED = "mentor_capacity_reached"
    MERGE_TOO_LARGE = "merge_too_large"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    NOT_DRAFT = "not_draft"
    NOT_ENROLLED = "not_enrolled"
    NOT_FOUND = "not_found"
    NOT_IMPLEMENTED = "not_implemented"
    ONBOARDING_REQUIRED = "onboarding_required"
    ORIGIN_MISMATCH = "origin_mismatch"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    PROJECT_FULL = "project_full"
    PROJECTS_LED_LIMIT = "projects_led_limit"
    RATE_LIMITED = "rate_limited"
    REFRESH_TOO_SOON = "refresh_too_soon"
    RESTRICTED = "restricted"
    RESULT_EXISTS = "result_exists"
    RESULT_NOT_DUE = "result_not_due"
    REVIEW_WINDOW_CLOSED = "review_window_closed"
    SEATS_CLOSE_BELOW_MIN = "seats_close_below_min"
    TEAM_FULL = "team_full"
    TELEGRAM_ALREADY_LINKED = "telegram_already_linked"
    TELEGRAM_NOT_LINKED = "telegram_not_linked"
    UNAUTHENTICATED = "unauthenticated"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    USER_DND = "user_dnd"
    VALIDATION_ERROR = "validation_error"


ERROR_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.ALREADY_ENROLLED: HTTPStatus.CONFLICT,
    ErrorCode.ALREADY_IN_TEAM: HTTPStatus.CONFLICT,
    ErrorCode.ALREADY_INVITED: HTTPStatus.CONFLICT,
    ErrorCode.ATTENDANCE_WINDOW_CLOSED: HTTPStatus.CONFLICT,
    ErrorCode.BAD_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.BANNED: HTTPStatus.FORBIDDEN,
    ErrorCode.CANNOT_CONFIRM_OWN_FACT: HTTPStatus.FORBIDDEN,
    ErrorCode.CHECKIN_LIMIT: HTTPStatus.CONFLICT,
    ErrorCode.CODE_EXPIRED: HTTPStatus.GONE,
    ErrorCode.CODE_INVALID: HTTPStatus.NOT_FOUND,
    ErrorCode.CONFIRMATION_WITHDRAW_CLOSED: HTTPStatus.CONFLICT,
    ErrorCode.CORE_FULL: HTTPStatus.CONFLICT,
    ErrorCode.ENROLLMENT_CLOSED: HTTPStatus.CONFLICT,
    ErrorCode.FILE_TOO_LARGE: HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    ErrorCode.FORMATION_IN_PROGRESS: HTTPStatus.CONFLICT,
    ErrorCode.HELP_REQUEST_OPEN: HTTPStatus.CONFLICT,
    ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INVALID_STATE: HTTPStatus.CONFLICT,
    ErrorCode.JOIN_REQUEST_PENDING: HTTPStatus.CONFLICT,
    ErrorCode.LAST_ADMIN: HTTPStatus.CONFLICT,
    ErrorCode.LEAD_FIT_PENDING: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.LEAD_FIT_TOO_LOW: HTTPStatus.FORBIDDEN,
    ErrorCode.LEAD_NOT_SWAPPABLE: HTTPStatus.CONFLICT,
    ErrorCode.MENTEE_LIMIT_REACHED: HTTPStatus.CONFLICT,
    ErrorCode.MENTOR_CAPACITY_REACHED: HTTPStatus.CONFLICT,
    ErrorCode.MERGE_TOO_LARGE: HTTPStatus.CONFLICT,
    ErrorCode.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
    ErrorCode.NOT_DRAFT: HTTPStatus.CONFLICT,
    ErrorCode.NOT_ENROLLED: HTTPStatus.CONFLICT,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.NOT_IMPLEMENTED: HTTPStatus.NOT_IMPLEMENTED,
    ErrorCode.ONBOARDING_REQUIRED: HTTPStatus.FORBIDDEN,
    ErrorCode.ORIGIN_MISMATCH: HTTPStatus.FORBIDDEN,
    ErrorCode.POSSIBLE_DUPLICATE: HTTPStatus.CONFLICT,
    ErrorCode.PROJECT_FULL: HTTPStatus.CONFLICT,
    ErrorCode.PROJECTS_LED_LIMIT: HTTPStatus.CONFLICT,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.REFRESH_TOO_SOON: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.RESTRICTED: HTTPStatus.FORBIDDEN,
    ErrorCode.RESULT_EXISTS: HTTPStatus.CONFLICT,
    ErrorCode.RESULT_NOT_DUE: HTTPStatus.CONFLICT,
    ErrorCode.REVIEW_WINDOW_CLOSED: HTTPStatus.CONFLICT,
    ErrorCode.SEATS_CLOSE_BELOW_MIN: HTTPStatus.CONFLICT,
    ErrorCode.TEAM_FULL: HTTPStatus.CONFLICT,
    ErrorCode.TELEGRAM_ALREADY_LINKED: HTTPStatus.CONFLICT,
    ErrorCode.TELEGRAM_NOT_LINKED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.UNAUTHENTICATED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.UNSUPPORTED_FILE_TYPE: HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.USER_DND: HTTPStatus.CONFLICT,
    ErrorCode.VALIDATION_ERROR: HTTPStatus.UNPROCESSABLE_ENTITY,
}


class ValidationIssue(Contract):
    loc: list[str | int]
    msg: str


class Problem(Contract):
    type: str
    title: str
    status: int
    detail: str | None = None
    code: ErrorCode
    errors: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        public_url: str,
        code: ErrorCode,
        detail: str | None = None,
        errors: list[ValidationIssue] | None = None,
    ) -> Self:
        return cls(
            type=f"{public_url.rstrip('/')}/errors/{code}",
            title=code.replace("_", " ").capitalize(),
            status=ERROR_STATUS[code].value,
            detail=detail,
            code=code,
            errors=errors if errors is not None else [],
        )


class Page[T](Contract):
    items: list[T]
    next_cursor: str | None

    @classmethod
    def model_parametrized_name(cls, params: tuple[type[Any], ...]) -> str:
        def name(parameter: Any) -> str:
            if get_origin(parameter) is Annotated:
                return name(get_args(parameter)[0])
            if get_origin(parameter) is UnionType:
                return "Or".join(name(item) for item in get_args(parameter))
            return parameter.__name__

        return "And".join(name(parameter) for parameter in params) + "Page"
