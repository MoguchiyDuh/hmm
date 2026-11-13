from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    HttpUrl,
    JsonValue,
    StringConstraints,
    model_validator,
)

from hmm_contracts.common import (
    AreaId,
    AreaImportance,
    AreaKey,
    Contract,
    EventFormat,
    EventId,
    EventKind,
    EventReviewReason,
    SourceId,
    SourcePersonRole,
    TimeZoneName,
    ValidationIssue,
)

__all__ = [
    "AreaListItem",
    "AreaListResponse",
    "Difficulty",
    "EventImport",
    "EventImportBatch",
    "EventImportResponse",
    "EventImportResult",
    "ImportArea",
    "Location",
    "MatchHint",
    "ParserHeartbeat",
    "ParserStats",
    "Prize",
    "Source",
    "SourcePerson",
    "TrackedEvent",
    "TrackedEventsResponse",
]

type _Confidence = Annotated[float, Field(ge=0, le=1)]
type _TeamSize = Annotated[int, Field(ge=1)]


class Source(Contract):
    source_id: SourceId
    urls: list[HttpUrl]
    fetched_at: AwareDatetime


class MatchHint(Contract):
    event_id: EventId


class Location(Contract):
    country: str | None = None
    city: str | None = None
    address: str | None = None


class ImportArea(Contract):
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    importance: AreaImportance
    headcount: int | None = None


class Prize(Contract):
    amount: float | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    description: str | None = None


class Difficulty(Contract):
    level: Annotated[float, Field(ge=1, le=10)]
    rationale: str


class SourcePerson(Contract):
    github_login: str | None = None
    telegram_username: str | None = None
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    role: SourcePersonRole
    place: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_place(self) -> Self:
        if self.place is not None and self.role != SourcePersonRole.WINNER:
            raise ValueError("Only a winner may have a place")
        return self


class EventImport(Contract):
    source: Source
    match_hint: MatchHint | None = None
    status: Literal["announced", "cancelled"] = "announced"
    kind: EventKind
    title: str
    summary: Annotated[str, Field(max_length=500)] | None = None
    description_md: Annotated[str, Field(max_length=20000)] | None = None
    organizer: str | None = None
    website_url: HttpUrl | None = None
    registration_url: HttpUrl | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    registration_opens_at: AwareDatetime | None = None
    registration_deadline: AwareDatetime | None = None
    timezone: TimeZoneName | None = None
    format: EventFormat | None = None
    location: Location | None = None
    language: list[Annotated[str, Field(pattern=r"^[a-z]{2}$")]] = Field(default_factory=list)
    eligibility: str | None = None
    themes: list[str] = Field(default_factory=list)
    areas: list[ImportArea] = Field(default_factory=list)
    team_size_min: _TeamSize | None = None
    team_size_max: _TeamSize | None = None
    prize: Prize | None = None
    difficulty: Difficulty | None = None
    people: list[SourcePerson] = Field(default_factory=list)
    confidence: _Confidence
    field_confidence: dict[str, _Confidence] = Field(default_factory=dict)
    inferred_fields: list[str] = Field(default_factory=list)
    extractor_version: str

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


class EventImportBatch(Contract):
    items: Annotated[list[JsonValue], Field(min_length=1, max_length=50)]

    @classmethod
    def of(cls, imports: Sequence[EventImport]) -> Self:
        return cls(items=[event.model_dump(mode="json") for event in imports])


class EventImportResult(Contract):
    index: Annotated[int, Field(ge=0)]
    source_id: SourceId | None = None
    result: Literal["created", "updated", "unchanged", "draft", "rejected"]
    event_id: EventId | None = None
    review_reasons: list[EventReviewReason] = Field(default_factory=list)
    ignored_fields: list[str] = Field(default_factory=list)
    errors: list[ValidationIssue] = Field(default_factory=list)


class EventImportResponse(Contract):
    results: list[EventImportResult]


class TrackedEvent(Contract):
    source_id: SourceId
    urls: list[HttpUrl]
    ends_at: AwareDatetime


class TrackedEventsResponse(Contract):
    items: list[TrackedEvent]
    next_cursor: str | None


class AreaListItem(Contract):
    id: AreaId
    key: AreaKey
    name: str
    description: str
    sort: int


class AreaListResponse(Contract):
    items: list[AreaListItem]
    next_cursor: str | None


class ParserStats(Contract):
    sources_checked: Annotated[int, Field(ge=0)]
    candidates: Annotated[int, Field(ge=0)]
    imported: Annotated[int, Field(ge=0)]
    errors: Annotated[int, Field(ge=0)]


class ParserHeartbeat(Contract):
    run_id: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    stats: ParserStats
    errors: list[JsonValue] = Field(default_factory=list)
