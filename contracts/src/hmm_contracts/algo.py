import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, SecretStr, StringConstraints

from hmm_contracts.common import (
    ActivityEntryId,
    ActivityType,
    AreaId,
    AreaImportance,
    AreaKey,
    AttendanceRole,
    AttendanceStatus,
    ConfirmationFactType,
    ConfirmationId,
    ConfirmationSource,
    Contract,
    EventFormat,
    EventId,
    EventKind,
    EventOrigin,
    FactId,
    FactType,
    FlagStatus,
    FlagSubjectType,
    FormationKind,
    HelpRequestFormat,
    MemberRole,
    MemberState,
    MentorshipCheckinId,
    MentorshipId,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    MilestoneId,
    OverrideEvidence,
    PlaceStatus,
    ProjectId,
    ProjectStatus,
    ProjectUpdateId,
    QuestionId,
    QuestionOptionId,
    QuizId,
    QuizStatus,
    ReleaseId,
    RestrictionKind,
    ResultOutcome,
    ReviewContext,
    ReviewId,
    ReviewSubjectType,
    Severity,
    SubjectId,
    TeamStatus,
    Trust,
    UserId,
)
from hmm_contracts.formation import Bounds

__all__ = [
    "AccountInfo",
    "ActivityEntry",
    "AlgoCache",
    "AlgoContext",
    "AlgoSettings",
    "Algorithm",
    "AnswerValue",
    "Area",
    "AreaScore",
    "CheckinIn",
    "CommunityGraph",
    "ConfirmationEdge",
    "ConfirmationIn",
    "Core",
    "Count",
    "EffectiveScore",
    "EnrolledMember",
    "EventArea",
    "EventSnapshot",
    "Fact",
    "FactAward",
    "FactContext",
    "FactEffort",
    "Flag",
    "FormationPlan",
    "FormationRequest",
    "GithubRepo",
    "GithubSummary",
    "LLMConfig",
    "LeadFit",
    "MemberEventSummary",
    "MemberHistory",
    "MemberMetrics",
    "MemberSnapshot",
    "MentorMatchRequest",
    "MentorOffer",
    "MentorshipContext",
    "MentorshipEdge",
    "MentorshipRole",
    "MilestoneIn",
    "MultiChoiceConfig",
    "MultiChoiceQuestion",
    "Nonnegative",
    "NumberConfig",
    "NumberQuestion",
    "OnboardingAnswer",
    "OnboardingAnswers",
    "OnboardingBaseline",
    "OnboardingRequest",
    "OnboardingScores",
    "PardonedFlag",
    "PlannedMember",
    "PlannedMerge",
    "PlannedTeam",
    "ProjectResultContext",
    "ProjectSnapshot",
    "ProjectUpdateIn",
    "QuestionOption",
    "QuestionType",
    "QuizDef",
    "QuizQuestion",
    "QuizQuestionBase",
    "QuizSection",
    "RankedMember",
    "RatingSummary",
    "Reason",
    "Rec",
    "RecommendRequest",
    "Recommendations",
    "Recruiting",
    "ReleaseContext",
    "ReleaseContribution",
    "ReserveRequest",
    "ReviewEdge",
    "ReviewIn",
    "ScaleConfig",
    "ScaleQuestion",
    "ScoreWithReason",
    "SingleChoiceConfig",
    "SingleChoiceQuestion",
    "TeamComposition",
    "TeamMemberSnapshot",
    "TextConfig",
    "TextQuestion",
    "UnitScore",
    "UrlConfig",
    "UrlQuestion",
    "VisibilityAll",
    "VisibilityAny",
    "VisibilityCondition",
    "VisibilityNot",
    "VisibilityOperator",
    "VisibilityPredicate",
]

type AreaScore = Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)]
type UnitScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Count = Annotated[int, Field(ge=0)]
type Reason = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"\S")]
type AnswerValue = str | list[str] | int | float


class AlgoSettings(Contract):
    activity_window_days: int = Field(default=14, gt=0)
    lead_fit_threshold: UnitScore = 0.6
    viable_ratio: Annotated[float, Field(gt=0, le=1)] = 0.75
    core_ratio: Annotated[float, Field(gt=0, le=1)] = 0.5
    team_size_default_min: int = Field(default=2, ge=1)
    team_size_default_max: int = Field(default=5, ge=1)
    mentor_min_score: AreaScore = 6.0
    mentor_min_gap: AreaScore = 2.0
    mentor_capacity_default: Count = 3
    mentor_notify_top_n: int = Field(default=5, gt=0)


class LLMConfig(Contract):
    base_url: str
    api_key: SecretStr
    model: str


class AlgoCache(Protocol):
    async def get(self, namespace: str, key: str) -> bytes | None: ...

    async def set(self, namespace: str, key: str, value: bytes, ttl: timedelta | None) -> None: ...

    async def delete(self, namespace: str, key: str) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AlgoContext:
    now: datetime
    settings: AlgoSettings
    llm: LLMConfig | None
    cache: AlgoCache
    log: logging.Logger


class Area(Contract):
    id: AreaId
    key: AreaKey
    name: str
    description: str
    sort: int
    archived_at: AwareDatetime | None


class QuestionType(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    SCALE = "scale"
    TEXT = "text"
    NUMBER = "number"
    URL = "url"


class VisibilityOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    INCLUDES = "includes"
    EXCLUDES = "excludes"
    INCLUDES_ANY = "includes_any"
    GTE = "gte"
    LTE = "lte"
    ANSWERED = "answered"
    NOT_ANSWERED = "not_answered"


class VisibilityPredicate(Contract):
    q: str
    op: VisibilityOperator
    value: AnswerValue | None = None


class VisibilityAll(Contract):
    all: list["VisibilityCondition"]


class VisibilityAny(Contract):
    any: list["VisibilityCondition"]


class VisibilityNot(Contract):
    condition: "VisibilityCondition" = Field(alias="not")


type VisibilityCondition = VisibilityPredicate | VisibilityAll | VisibilityAny | VisibilityNot


class SingleChoiceConfig(Contract):
    allow_other: bool


class MultiChoiceConfig(Contract):
    min: Count
    max: Count
    allow_other: bool


class ScaleConfig(Contract):
    min: int
    max: int
    min_label: str
    max_label: str


class TextConfig(Contract):
    max_length: int = Field(gt=0)
    multiline: bool


class NumberConfig(Contract):
    min: float | None
    max: float | None
    unit: str | None


class UrlConfig(Contract):
    pass


class QuestionOption(Contract):
    id: QuestionOptionId
    key: str
    label: str
    position: int
    area_hints: dict[AreaKey, UnitScore]


class QuizQuestionBase(Contract):
    id: QuestionId
    section: str
    key: str
    text: str
    help: str | None
    required: bool
    position: int
    visible_if: VisibilityCondition | None
    options: list[QuestionOption]


class SingleChoiceQuestion(QuizQuestionBase):
    type: Literal[QuestionType.SINGLE_CHOICE]
    config: SingleChoiceConfig


class MultiChoiceQuestion(QuizQuestionBase):
    type: Literal[QuestionType.MULTI_CHOICE]
    config: MultiChoiceConfig


class ScaleQuestion(QuizQuestionBase):
    type: Literal[QuestionType.SCALE]
    config: ScaleConfig


class TextQuestion(QuizQuestionBase):
    type: Literal[QuestionType.TEXT]
    config: TextConfig


class NumberQuestion(QuizQuestionBase):
    type: Literal[QuestionType.NUMBER]
    config: NumberConfig


class UrlQuestion(QuizQuestionBase):
    type: Literal[QuestionType.URL]
    config: UrlConfig


type QuizQuestion = Annotated[
    SingleChoiceQuestion
    | MultiChoiceQuestion
    | ScaleQuestion
    | TextQuestion
    | NumberQuestion
    | UrlQuestion,
    Field(discriminator="type"),
]


class QuizSection(Contract):
    key: str
    position: int
    visible_if: VisibilityCondition | None


class QuizDef(Contract):
    id: QuizId
    version: int = Field(ge=1)
    title: str
    status: QuizStatus
    published_at: AwareDatetime | None
    sections: list[QuizSection]
    questions: list[QuizQuestion]


class OnboardingAnswer(Contract):
    value: AnswerValue
    other: str | None = None


class OnboardingAnswers(Contract):
    answers: dict[str, OnboardingAnswer]


class GithubRepo(Contract):
    name: str
    description: str | None
    topics: list[str]
    language: str | None
    stars: Count
    pushed_at: AwareDatetime | None


class GithubSummary(Contract):
    login: str
    account_created_at: AwareDatetime
    languages: dict[str, Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]]
    repos: list[GithubRepo]
    followers: Count
    contributions_last_year: Count
    public_repos: Count
    stars_total: Count


class OnboardingRequest(Contract):
    quiz: QuizDef
    answers: OnboardingAnswers
    cv_text: str | None
    github: GithubSummary | None
    areas: list[Area]


class ScoreWithReason(Contract):
    value: AreaScore
    reason: Reason


class OnboardingScores(Contract):
    area_scores: dict[AreaKey, ScoreWithReason]
    discrepancies: list[str]


class EffectiveScore(Contract):
    calculated: AreaScore
    override: AreaScore | None
    override_evidence: OverrideEvidence | None
    effective: AreaScore


class RatingSummary(Contract):
    avg: Annotated[float, Field(ge=1, le=10, allow_inf_nan=False)] | None
    count: Count


class MentorOffer(Contract):
    area: AreaKey
    topics: str
    active: bool


class MemberSnapshot(Contract):
    user_id: UserId
    display_name: str
    about_md: str
    cv_text: str | None
    github: GithubSummary | None
    area_scores: dict[AreaKey, EffectiveScore]
    scores_reviewed_at: AwareDatetime | None
    xp: Annotated[float, Field(allow_inf_nan=False)]
    activity: Nonnegative
    hackathons_finished: Count
    times_led: Count
    projects_shipped: Count
    mentorships_completed: Count
    reputation: dict[ReviewContext, RatingSummary]
    mentor_offers: list[MentorOffer]
    mentor_capacity: Count
    mentor_active: Count
    joined_at: AwareDatetime
    mentor_notified_at: list[AwareDatetime] = Field(default_factory=list)


class OnboardingBaseline(OnboardingScores):
    submitted_at: AwareDatetime


class EventArea(Contract):
    area: AreaKey
    importance: AreaImportance
    headcount: int | None = Field(ge=1)


class EventSnapshot(Contract):
    id: EventId
    title: str
    summary: str | None
    description_md: str | None
    themes: list[str]
    format: EventFormat | None
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    areas: list[EventArea]
    team_size_min: int | None = Field(ge=1)
    team_size_max: int | None = Field(ge=1)
    difficulty: Annotated[float, Field(ge=1, le=10, allow_inf_nan=False)] | None
    kind: EventKind
    origin: EventOrigin
    created_by: UserId | None


class Recruiting(Contract):
    areas: list[AreaKey]
    note: str


class ProjectSnapshot(Contract):
    id: ProjectId
    name: str
    goal: str
    description_md: str
    areas: list[AreaKey]
    recruiting: Recruiting | None
    event_id: EventId | None
    status: ProjectStatus
    team_status: TeamStatus | None
    links: list[str]


class ConfirmationIn(Contract):
    id: ConfirmationId
    confirmer_id: UserId | None
    source: ConfirmationSource
    confirmer_account_age: timedelta | None
    confirmer_xp: float | None
    confirmer_restrictions: list[RestrictionKind]
    ignored: bool
    flag_status: FlagStatus | None
    created_at: AwareDatetime
    withdrawn_at: AwareDatetime | None


class ProjectResultContext(Contract):
    outcome: ResultOutcome
    place: int | None = Field(ge=1)
    award: str | None
    place_status: PlaceStatus | None
    participants: dict[UserId, bool]
    member_areas: dict[UserId, AreaKey]
    repo_url: str | None
    demo_url: str | None
    presentation_url: str | None
    retrospective_md: str
    auto_closed: bool
    submitted_at: AwareDatetime | None


class ReleaseContribution(Contract):
    user_id: UserId
    contributed: bool
    area: AreaKey | None


class ReleaseContext(Contract):
    id: ReleaseId
    title: str
    notes_md: str
    repo_url: str | None
    demo_url: str | None
    writeup_url: str | None
    created_at: AwareDatetime
    contributions: list[ReleaseContribution]


class ProjectUpdateIn(Contract):
    id: ProjectUpdateId
    author_id: UserId
    body_md: str
    links: list[str]
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None


class MilestoneIn(Contract):
    id: MilestoneId
    title: str
    description: str | None
    due_on: date | None
    done_by: UserId | None
    done_at: AwareDatetime | None


class CheckinIn(Contract):
    id: MentorshipCheckinId
    author_id: UserId
    note: str
    occurred_on: date
    minutes: int | None = Field(ge=0)


class FactEffort(Contract):
    updates: list[ProjectUpdateIn]
    milestones: list[MilestoneIn]
    checkins: list[CheckinIn]
    minutes: Count


class MentorshipRole(StrEnum):
    MENTOR = "mentor"
    MENTEE = "mentee"
    PEER = "peer"


class MentorshipContext(Contract):
    id: MentorshipId
    kind: MentorshipKind
    status: MentorshipStatus
    mentor_id: UserId
    learner_id: UserId
    area: AreaKey
    mentor_score_at_start: AreaScore
    learner_score_at_start: AreaScore
    learner_score_now: AreaScore
    started_at: AwareDatetime
    closed_at: AwareDatetime | None
    duration: timedelta
    mentor_checkin_count: Count
    learner_checkin_count: Count
    earlier_pair_mentorships: Count
    outcome: MentorshipOutcome | None
    summary: str | None
    mentor_acknowledged_at: AwareDatetime | None
    endorsement: str | None


class FactContext(Contract):
    event: EventSnapshot | None
    project: ProjectSnapshot | None
    result: ProjectResultContext | None
    releases: list[ReleaseContext]
    mentorship: MentorshipContext | None
    attendance_status: AttendanceStatus | None


class PardonedFlag(Contract):
    subject_type: FlagSubjectType
    subject_id: SubjectId
    reason: Reason


class Fact(Contract):
    type: FactType
    id: FactId
    role: AttendanceRole | MemberRole | MentorshipRole | None
    area: AreaKey | None
    trust: Trust
    flag_status: FlagStatus | None
    excluded: bool
    proof_links: list[str] = Field(max_length=5)
    note: str
    created_at: AwareDatetime
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    confirmed_at: AwareDatetime | None
    left_at: AwareDatetime | None
    confirmations: list[ConfirmationIn]
    counterparts: list[UserId]
    outcome: ResultOutcome | ProjectStatus | MentorshipOutcome | None
    effort: FactEffort
    context: FactContext


class ActivityEntry(Contract):
    id: ActivityEntryId
    type: ActivityType
    title: str = Field(max_length=200)
    description_md: str = Field(max_length=5000)
    links: list[str] = Field(max_length=10)
    area: AreaKey | None
    occurred_on: date
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None
    flag_status: FlagStatus | None
    excluded: bool


class ReviewIn(Contract):
    id: ReviewId
    reviewer_id: UserId
    reviewee_id: UserId
    context: ReviewContext
    rating: int | None = Field(ge=1, le=10)
    subject_type: ReviewSubjectType
    subject_id: SubjectId
    comment: str = Field(max_length=2000)
    created_at: AwareDatetime
    revealed_at: AwareDatetime | None
    removed_at: AwareDatetime | None
    flag_status: FlagStatus | None


class MemberHistory(MemberSnapshot):
    onboarding: OnboardingBaseline | None
    facts: list[Fact]
    activity_entries: list[ActivityEntry]
    reviews_received: list[ReviewIn]
    xp_adjustments_total: float
    pardoned: list[PardonedFlag] = Field(default_factory=list)


class FactAward(Contract):
    fact_type: FactType
    fact_id: FactId
    xp: Nonnegative
    activity: Nonnegative
    reason: Reason


class Flag(Contract):
    subject_type: FlagSubjectType
    subject_id: SubjectId
    related_user_ids: list[UserId]
    reason: Reason
    severity: Severity


class MemberMetrics(Contract):
    area_scores: dict[AreaKey, ScoreWithReason]
    xp: Nonnegative
    activity: Nonnegative
    fact_awards: list[FactAward]
    flags: list[Flag]


class LeadFit(Contract):
    user_id: UserId
    fit: UnitScore
    reason: Reason


class TeamMemberSnapshot(MemberSnapshot):
    role: MemberRole
    area: AreaKey | None
    state: MemberState
    willing_to_lead: bool
    claimed_lead: bool
    enrolled_at: AwareDatetime


class EnrolledMember(MemberSnapshot):
    preferred_area: AreaKey | None
    willing_to_lead: bool
    enrolled_at: AwareDatetime
    note: str = Field(max_length=1000)


class RecommendRequest(Contract):
    event: EventSnapshot | None
    project: ProjectSnapshot
    team: list[TeamMemberSnapshot]
    enrolled: list[EnrolledMember]
    others: list[MemberSnapshot]
    limit_enrolled: Count
    limit_others: Count


class Rec(Contract):
    user_id: UserId
    score: UnitScore
    reasons: list[Reason] = Field(min_length=1, max_length=3)
    tags: list[str]
    suggested_area: AreaKey | None


class Recommendations(Contract):
    enrolled: list[Rec]
    others: list[Rec]


class Core(Contract):
    project_id: ProjectId
    lead: TeamMemberSnapshot
    members: list[TeamMemberSnapshot]


class FormationRequest(Contract):
    hackathon: EventSnapshot
    kind: FormationKind
    target: int = Field(ge=1)
    viable: int = Field(ge=1)
    max_size: int = Field(ge=1)
    bounds: Bounds
    cores: list[Core]
    unplaced: list[EnrolledMember]
    lead_fits: dict[UserId, LeadFit]


class PlannedMember(Contract):
    user_id: UserId
    area: AreaKey | None
    reason: Reason


class PlannedTeam(Contract):
    project_id: ProjectId | None
    lead_user_id: UserId | None
    members: list[PlannedMember]
    reason: Reason


class RankedMember(Contract):
    user_id: UserId
    rank: int = Field(ge=1)
    reason: Reason


class PlannedMerge(Contract):
    absorbed_team_id: ProjectId
    target_team_id: ProjectId
    reason: Reason


class FormationPlan(Contract):
    teams: list[PlannedTeam]
    reserve: list[RankedMember]
    merges: list[PlannedMerge]
    summary: str = Field(min_length=1, pattern=r"\S")


class TeamComposition(Contract):
    project_id: ProjectId
    members: list[TeamMemberSnapshot]
    open_seats: Count


class ReserveRequest(Contract):
    hackathon: EventSnapshot
    teams: list[TeamComposition]
    reserve: list[EnrolledMember]


class MentorMatchRequest(Contract):
    area: Area
    topic: str = Field(max_length=200)
    goal: str = Field(max_length=1000)
    format: HelpRequestFormat
    learner: MemberSnapshot
    candidates: list[MemberSnapshot]
    limit: Count


class ConfirmationEdge(Contract):
    id: ConfirmationId
    confirmer_id: UserId | None
    subject_user_id: UserId
    fact_type: ConfirmationFactType
    fact_id: FactId
    context_id: SubjectId
    source: ConfirmationSource
    created_at: AwareDatetime
    ignored: bool
    withdrawn_at: AwareDatetime | None
    flag_status: FlagStatus | None


class MentorshipEdge(Contract):
    id: MentorshipId
    mentor_id: UserId
    learner_id: UserId
    area: AreaKey
    kind: MentorshipKind
    outcome: MentorshipOutcome | None
    started_at: AwareDatetime
    closed_at: AwareDatetime | None


class ReviewEdge(Contract):
    id: ReviewId
    reviewer_id: UserId
    reviewee_id: UserId
    context: ReviewContext
    rating: int | None = Field(ge=1, le=10)
    subject_type: ReviewSubjectType
    subject_id: SubjectId
    created_at: AwareDatetime
    revealed_at: AwareDatetime | None
    removed_at: AwareDatetime | None
    flag_status: FlagStatus | None


class MemberEventSummary(Contract):
    id: EventId
    creator_id: UserId
    created_at: AwareDatetime
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    attendees: list[UserId]
    confirmations: list[ConfirmationEdge]


class AccountInfo(Contract):
    joined_at: AwareDatetime
    xp: float
    github_account_age: timedelta | None
    restrictions: list[RestrictionKind]


class CommunityGraph(Contract):
    confirmations: list[ConfirmationEdge]
    mentorships: list[MentorshipEdge]
    reviews: list[ReviewEdge]
    member_events: list[MemberEventSummary]
    accounts: dict[UserId, AccountInfo]
    pardoned: list[PardonedFlag]


class Algorithm(Protocol):
    version: str

    async def score_onboarding(
        self, ctx: AlgoContext, request: OnboardingRequest
    ) -> OnboardingScores: ...

    async def compute_member(
        self, ctx: AlgoContext, member: MemberHistory, areas: list[Area]
    ) -> MemberMetrics: ...

    async def lead_fit(
        self, ctx: AlgoContext, hackathon: EventSnapshot, candidates: list[MemberSnapshot]
    ) -> list[LeadFit]: ...

    async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations: ...

    async def form_teams(self, ctx: AlgoContext, request: FormationRequest) -> FormationPlan: ...

    async def rank_reserve(
        self, ctx: AlgoContext, request: ReserveRequest
    ) -> list[RankedMember]: ...

    async def match_mentors(self, ctx: AlgoContext, request: MentorMatchRequest) -> list[Rec]: ...

    async def scan_community(self, ctx: AlgoContext, graph: CommunityGraph) -> list[Flag]: ...
