from enum import StrEnum
from typing import Annotated, Self

from hmm_contracts.common import (
    AreaImportance,
    AreaKey,
    EventFormat,
    EventId,
    EventKind,
    EventOrigin,
    EventPhase,
    EventReviewReason,
    EventRevisionId,
    EventRevisionOrigin,
    EventSourceId,
    EventStatus,
    FormationPhase,
    SourceId,
    TimeZoneName,
    UserId,
)
from hmm_contracts.imports import Location, Prize
from pydantic import AwareDatetime, Field, HttpUrl, JsonValue, model_validator

from hmm_core.api.schemas.attendance import Attendance
from hmm_core.api.schemas.common import (
    ApiInput,
    ApiModel,
    AreaRef,
    EventAction,
    LongMarkdown,
    Name,
    Note,
    ReasonBody,
    Slug,
    Title,
    UserRef,
    WithAllowedActions,
)
from hmm_core.api.schemas.formation import Formation, LeadEligibility


class EventFieldLockAction(StrEnum):
    UNLOCK = "unlock"


class EventAreaBody(ApiInput):
    key: AreaKey
    importance: AreaImportance
    headcount: int | None = Field(default=None, ge=1)


class EventAreaDetail(AreaRef):
    importance: AreaImportance
    headcount: int | None = Field(ge=1)


class CreateEventBody(ReasonBody):
    slug: Slug | None = None
    kind: EventKind
    formation_enabled: bool | None = None
    title: Title
    summary: str | None = Field(default=None, max_length=500)
    description_md: LongMarkdown | None = None
    organizer: Title | None = None
    website_url: HttpUrl | None = None
    registration_url: HttpUrl | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    registration_opens_at: AwareDatetime | None = None
    registration_deadline: AwareDatetime | None = None
    timezone: TimeZoneName
    format: EventFormat | None = None
    location: Location | None = None
    language: list[Annotated[str, Field(pattern=r"^[a-z]{2}$")]] = Field(default_factory=list)
    eligibility: Note | None = None
    themes: list[Name] = Field(default_factory=list)
    areas: list[EventAreaBody] = Field(default_factory=list)
    team_size_min: int | None = Field(default=None, ge=1)
    team_size_max: int | None = Field(default=None, ge=1)
    prize: Prize | None = None
    difficulty: float | None = Field(default=None, ge=1, le=10)
    difficulty_rationale: Note | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.ends_at < self.starts_at:
            raise ValueError("ends_at must not precede starts_at")
        if (
            self.team_size_min is not None
            and self.team_size_max is not None
            and self.team_size_min > self.team_size_max
        ):
            raise ValueError("team_size_min must not exceed team_size_max")
        return self


class UpdateEventBody(ReasonBody):
    slug: Slug | None = None
    kind: EventKind | None = None
    formation_enabled: bool | None = None
    title: Title | None = None
    summary: str | None = Field(default=None, max_length=500)
    description_md: LongMarkdown | None = None
    organizer: Title | None = None
    website_url: HttpUrl | None = None
    registration_url: HttpUrl | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    registration_opens_at: AwareDatetime | None = None
    registration_deadline: AwareDatetime | None = None
    timezone: TimeZoneName | None = None
    format: EventFormat | None = None
    location: Location | None = None
    language: list[Annotated[str, Field(pattern=r"^[a-z]{2}$")]] | None = None
    eligibility: Note | None = None
    themes: list[Name] | None = None
    areas: list[EventAreaBody] | None = None
    team_size_min: int | None = Field(default=None, ge=1)
    team_size_max: int | None = Field(default=None, ge=1)
    prize: Prize | None = None
    difficulty: float | None = Field(default=None, ge=1, le=10)
    difficulty_rationale: Note | None = None
    status: EventStatus | None = None
    review_reasons: list[EventReviewReason] | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at < self.starts_at
        ):
            raise ValueError("ends_at must not precede starts_at")
        if (
            self.team_size_min is not None
            and self.team_size_max is not None
            and self.team_size_min > self.team_size_max
        ):
            raise ValueError("team_size_min must not exceed team_size_max")
        return self


class AttendanceCounts(ApiModel):
    going: int = Field(ge=0)
    attended: int = Field(ge=0)


class EventSummary(WithAllowedActions[EventAction]):
    id: EventId
    slug: str
    kind: EventKind
    formation_enabled: bool
    title: str
    summary: str | None
    organizer: str | None
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    registration_opens_at: AwareDatetime | None
    registration_deadline: AwareDatetime | None
    timezone: TimeZoneName
    format: EventFormat | None
    location: Location | None
    areas: list[EventAreaDetail]
    difficulty: float | None = Field(ge=1, le=10)
    status: EventStatus
    phase: EventPhase
    participation_phase: FormationPhase | None
    origin: EventOrigin
    attendance_counts: AttendanceCounts
    my_attendance: Attendance | None


class EventSource(ApiModel):
    id: EventSourceId
    source_id: SourceId
    urls: list[HttpUrl]
    extractor_version: str
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime


class EventDetail(EventSummary):
    description_md: str | None
    website_url: HttpUrl | None
    registration_url: HttpUrl | None
    language: list[str]
    eligibility: str | None
    themes: list[str]
    team_size_min: int | None = Field(ge=1)
    team_size_max: int | None = Field(ge=1)
    prize: Prize | None
    difficulty_rationale: str | None
    review_reasons: list[EventReviewReason]
    created_by: UserId | None
    confidence: float | None = Field(ge=0, le=1)
    unmapped_areas: list[str]
    sources: list[EventSource]
    members_going: list[UserRef]
    formation: Formation | None
    my_lead_fit: LeadEligibility | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EventFieldLock(WithAllowedActions[EventFieldLockAction]):
    event_id: EventId
    field: str
    locked_by: UserId
    locked_at: AwareDatetime


class EventRevision(ApiModel):
    id: EventRevisionId
    event_id: EventId
    origin: EventRevisionOrigin
    actor_id: UserId | None
    diff: dict[str, JsonValue]
    ignored_fields: list[str]
    created_at: AwareDatetime


class MergeEventBody(ReasonBody):
    into: EventId
