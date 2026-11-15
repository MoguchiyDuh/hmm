# 18. Algorithm package

## 18.1 Scope

The algorithm owns **behavior**: formulas, heuristics, team packing, LLM use, abuse detection. The rest of the app owns data loading, persistence, scheduling and hard-constraint validation. The implementation lives under `hmm.algo`, imports only `hmm.common`, and is called by the worker only.

## 18.2 Interface (`hmm.algo.models`)

```python
class Algorithm(Protocol):
    version: str

    async def score_onboarding(self, ctx: AlgoContext, request: OnboardingRequest) -> OnboardingScores: ...
    async def compute_member(self, ctx: AlgoContext, member: MemberHistory, areas: list[Area]) -> MemberMetrics: ...
    async def recommend(self, ctx: AlgoContext, request: RecommendRequest) -> Recommendations: ...
    async def form_teams(self, ctx: AlgoContext, request: FormationRequest) -> FormationPlan: ...
    async def match_mentors(self, ctx: AlgoContext, request: MentorMatchRequest) -> list[Rec]: ...
    async def scan_community(self, ctx: AlgoContext, graph: CommunityGraph) -> list[Flag]: ...


def get_algorithm() -> Algorithm: ...
```

Context provided by core:

```python
class AlgoContext:
    now: datetime
    settings: AlgoSettings        # activity_window_days, mentor_notify_top_n, etc.
    llm: LLMConfig | None         # base_url, api_key, model (chat only, no embeddings); None = disabled
    cache: AlgoCache              # get/set/delete bytes by (namespace, key, ttl), backed by algo_cache table
    log: Logger
```

Key I/O models (full definitions in `hmm.algo.models`; abbreviated here):

```python
class OnboardingRequest:
    quiz: QuizDef; answers: OnboardingAnswers          # visible answers only
    cv_text: str | None; github: GithubSummary | None
    areas: list[Area]

class OnboardingScores:
    area_scores: dict[AreaKey, ScoreWithReason]        # 0..10
    discrepancies: list[str]                           # hints for the admin member review
class ScoreWithReason:    value: float; reason: str

class MemberSnapshot:
    user_id: UUID; display_name: str; about_md: str; cv_text: str | None
    github: GithubSummary | None          # languages pct, repos[name, description, topics, language, stars, pushed_at], followers, contributions_last_year
    area_scores: dict[AreaKey, EffectiveScore]   # calculated, override, override_evidence, effective
    scores_reviewed_at: datetime | None
    xp: float; activity: float
    hackathons_finished: int; times_led: int; projects_shipped: int; mentorships_completed: int
    reputation: dict[Context, RatingSummary]     # member, lead, mentor, host -> avg, count
    mentor_offers: list[MentorOffer]             # area, topics
    mentor_capacity: int; mentor_active: int     # global, across areas
    joined_at: datetime

class MemberHistory(MemberSnapshot):
    onboarding: OnboardingBaseline | None        # OnboardingScores + submitted_at
    facts: list[Fact]                            # every fact (§25): type, id, role, area, trust, flag_status, dates,
                                                 #   confirmations [confirmer_id, source, confirmer account age, xp, ignored],
                                                 #   counterparts, outcome, effort (updates, milestones, check-ins, minutes),
                                                 #   context (event snapshot, project snapshot, mentorship kind + scores at start)
    activity_entries: list[ActivityEntry]        # solo log, flag_status
    reviews_received: list[ReviewIn]             # context, rating, subject; never for xp
    xp_adjustments_total: float

class MemberMetrics:
    area_scores: dict[AreaKey, ScoreWithReason]  # new calculated values (all areas)
    xp: float                                    # excluding admin adjustments
    activity: float
    fact_awards: list[FactAward]                 # fact_type, fact_id, xp, activity, reason
    flags: list[Flag]                            # subject_type, subject_id, related_user_ids, reason, severity

class EventSnapshot:
    id; title; summary; description_md; themes; format; starts_at; ends_at
    areas: list[EventArea]                   # area key, importance, headcount
    team_size_min; team_size_max; difficulty: float | None

class RecommendRequest:
    event: EventSnapshot | None                  # set for hackathon teams and event-linked projects
    project: ProjectSnapshot                     # name, goal, description, areas, recruiting areas + note
    team: list[TeamMemberSnapshot]               # MemberSnapshot + role + assigned area + state
    enrolled: list[EnrolledMember]               # MemberSnapshot + preferred_area + willing_to_lead + queued_at
    others: list[MemberSnapshot]
    limit_enrolled: int; limit_others: int
class Recommendations:    enrolled: list[Rec]; others: list[Rec]
class Rec:                user_id: UUID; score: float; reasons: list[str]; tags: list[str]; suggested_area: AreaKey | None

class FormationRequest:
    hackathon: EventSnapshot
    target: int; min_size: int; max_size: int
    units: list[FormationUnit]                   # every open unit (solo or open party), in queue order
class FormationUnit:      id: UUID; members: list[EnrolledMember]   # id = user_id for a solo, party_id for a party

class FormationPlan:
    teams: list[PlannedTeam]
    summary: str                                 # shown to admins
class PlannedTeam:        units: list[UUID]; reason: str   # units placed together; reason why this composition

class MentorMatchRequest:
    area: Area; topic: str; goal: str; format: str
    learner: MemberSnapshot
    candidates: list[MemberSnapshot]             # with mentor_offers and free capacity
    limit: int

class CommunityGraph:
    confirmations: list[ConfirmationEdge]        # confirmer, subject user, fact type, context id, created_at
    mentorships: list[MentorshipEdge]            # mentor, learner, area, kind, outcome, dates
    reviews: list[ReviewEdge]                    # reviewer, reviewee, context, rating, subject
    member_events: list[MemberEventSummary]      # creator, created_at, attendees, confirmations
    accounts: dict[UUID, AccountInfo]            # joined_at, xp, github account age
    pardoned: list[PardonedFlag]                 # subject + reason pairs not to re-flag
```

## 18.3 Behavioral requirements

1. **Pure with respect to core state.** No DB, no network except the configured LLM endpoint. Persistent memory only via `ctx.cache`.
2. **Deterministic results.** Given identical inputs and cache, scores, orderings and formation plans are identical. LLM output may only influence reasons text or be cached so that repeat runs reproduce.
3. **Graceful degradation.** `ctx.llm is None` or LLM errors → still return valid results (heuristic only) within timeout.
4. **Bounded output.** Scores 0..10 (areas), recs 0..1, every area key present in `area_scores` outputs, only ids from inputs, non-empty reasons, `len(reasons) ≤ 3`, each ≤ 200 chars, English.
5. **Recompute-from-scratch.** `compute_member` derives everything from `MemberHistory`. It never needs previous outputs.
6. **Separation.** Ratings from reviews must not influence `xp`. They may influence `recommend`, `form_teams` and `match_mentors`.
7. **Overrides respected.** Everything except `compute_member` uses `effective` scores. `compute_member` outputs `calculated` only. Reviewed scores (`scores_reviewed_at`, override evidence) may be weighted as more reliable.
8. **Explanations.** Every score, recommendation and planned team has a human-readable reason. A member must understand why they were placed where they were.
9. **Onboarding scoring.** Combines quiz answers (using `area_hints`), CV text and GitHub evidence. Returns discrepancy hints when sources disagree.
10. **Quick-pick goals.** Cover hackathon areas not yet covered by the team (importance `primary` > `required` > `nice`), respect headcounts, prefer recent activity and reserve members, and mix in beginners (low XP / few hackathons) when the team already has a strong member in that area. The exact mix is an implementation choice; it must be explainable through tags and reasons.
11. **Formation goals.** The plan must satisfy every hard constraint in §12.5: every unit appears once and whole, every team size is in [`min`, `max`], and it places at least as many people as `pack()`. Within that:
    - every team covers the hackathon's primary areas, then required ones, then nice ones;
    - strong members are spread across teams instead of stacked in one;
    - each team pairs beginners with an experienced member in the same area where possible;
    - preferred areas are honored where possible;
    - parties travel as one unit and are never split;
    - placing more people comes before building more teams;
    - the lead is picked by core, not by the plan (§12.7).
    Core rejects plans that break a hard constraint.
12. **Points goals** (§25.3).
    - Second-party rule: `self` facts give activity and at most a token amount of XP.
    - Award scales with trust, role (speaker > attendee, lead > member, mentor > learner), effort and outcome (shipped, place, goal reached). A place counts only with `place_status = verified` (§13).
    - Hackathon team members marked `participants = false` in the result get no result-based award.
    - Diminishing returns for repeated counterparts, the same kind in a short window, and reciprocal pairs.
    - Confirmations weigh by the confirmer's account age, XP and restriction state; `ignored` confirmations count zero.
    - Hackathon results, standalone releases and attendance of the same event are not double-counted.
    - Facts that are `to_check` or excluded by a penalty get zero.
    - Every award has a reason a member can read ("Speaker at X, confirmed by organizer: +40 XP").
13. **Mentor matching goals.** Strength in the area relative to the learner, mentor offers and topics matching the request, mentor reputation, recent activity, free capacity, a spread of notifications across mentors so the same people aren't always asked, and newer mentors mixed in when they're strong enough.
14. **Community scan goals.** Find rings of mutual confirmations, reciprocal mentoring, rating rings, clusters of new accounts confirming each other, and bursts of member-created events with only each other attending. Flag with `related_user_ids` and a reason an admin can verify. Never re-flag pardoned subject/reason pairs.
15. **Abuse detection (per member).** Flag implausible facts and entries (bursts, duplicate or near-duplicate text, links not resolving to the member's GitHub, claimed roles with no evidence, areas with no other evidence). Flags only; penalties are an admin decision (§10). `flag_status=pardoned` subjects must not be re-flagged for the same reason.
16. **Performance.** Up to 1 000 members, 200 candidates per `recommend` and `match_mentors`, every open unit per `form_teams`, a 180-day community graph for `scan_community`: within §17 timeouts on a modest single host. LLM results cached via `ctx.cache` keyed by content hash.
17. **Version.** `version` string changes whenever behavior changes. Core stores it with every output.

## 18.4 Production bar

- One implementation, shipped production-ready. No placeholder stage in deployment.
- A deterministic heuristic core is fully functional on its own; the LLM refines ranking and reasons when available.
- `hmm.algo.fake.FakeAlgorithm` exists only for the test suite and for local development before the real implementation lands. It is never wired into the deployed app.

## 18.5 Deliverables

- A production `Algorithm` implementation under `hmm.algo`, plus `hmm.algo.fake.FakeAlgorithm` (its `form_teams` returns `pack()`'s new teams).
- Offline evaluation harness over `tests/fixtures/members/*.json`, `events/*.json`, `formation/*.json`, `mentorship/*.json` and `community/*.json` (incl. planted rings), printing rankings, plans, awards, flags and reasons.
- A section in this document describing formulas and team composition strategy, so admins can understand outcomes.
- `tests/test_algo_conformance.py` passes: `pack()` examples and property test, ID subsets, determinism, no-LLM mode, plan validity against §12.5.
