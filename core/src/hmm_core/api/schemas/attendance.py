from hmm_contracts.common import (
    AreaKey,
    AttendanceId,
    AttendanceRole,
    AttendanceStatus,
    EventFormat,
    EventId,
    EventKind,
    EventOrigin,
    EventReviewReason,
    EventStatus,
    Trust,
    UserId,
)
from hmm_contracts.imports import Location
from pydantic import AwareDatetime, Field, HttpUrl

from hmm_core.api.schemas.common import ApiInput, EventRef, UserRef, WithAllowedActions


class SetAttendanceBody(ApiInput):
    status: AttendanceStatus
    role: AttendanceRole = AttendanceRole.ATTENDEE
    proof_links: list[HttpUrl] = Field(default_factory=list, max_length=5)
    note: str | None = None


class Attendance(WithAllowedActions):
    id: AttendanceId
    event_id: EventId
    user_id: UserId
    event: EventRef
    user: UserRef
    status: AttendanceStatus
    role: AttendanceRole
    proof_links: list[HttpUrl] = Field(max_length=5)
    note: str | None
    trust: Trust
    status_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CreateMemberEventBody(ApiInput):
    kind: EventKind
    title: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    format: EventFormat
    location: Location | None = None
    website_url: HttpUrl | None = None
    description_md: str | None = None
    area_keys: list[AreaKey] = Field(default_factory=list)


class UpdateMemberEventBody(ApiInput):
    kind: EventKind | None = None
    title: str | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    format: EventFormat | None = None
    location: Location | None = None
    website_url: HttpUrl | None = None
    description_md: str | None = None
    area_keys: list[AreaKey] | None = None


class CancelMemberEventBody(ApiInput):
    reason: str


class MemberEvent(EventRef, WithAllowedActions):
    slug: str
    summary: str | None
    description_md: str | None
    format: EventFormat
    location: Location | None
    website_url: HttpUrl | None
    organizer: str | None
    registration_url: HttpUrl | None
    registration_opens_at: AwareDatetime | None
    registration_deadline: AwareDatetime | None
    timezone: str
    language: list[str]
    eligibility: str | None
    themes: list[str]
    team_size_min: int | None = Field(ge=1, le=20)
    team_size_max: int | None = Field(ge=1, le=20)
    area_keys: list[AreaKey]
    status: EventStatus
    origin: EventOrigin
    created_by: UserId | None
    formation_enabled: bool
    review_reasons: list[EventReviewReason]
    created_at: AwareDatetime
    updated_at: AwareDatetime
