import json
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import get_type_hints
from uuid import UUID

import pytest
from hmm_contracts import algo as a
from hmm_contracts.common import Contract, FactType
from hmm_contracts.formation import Bounds
from pydantic import ValidationError

NOW = datetime(2026, 11, 12, tzinfo=UTC)
USER = UUID(int=1)
OTHER = UUID(int=2)
PROJECT = UUID(int=3)
EVENT = UUID(int=4)
REASON = "Covers the backend gap with confirmed delivery experience"


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    async def get(self, namespace: str, key: str) -> bytes | None:
        return self.values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: bytes, ttl: timedelta | None) -> None:
        self.values[namespace, key] = value

    async def delete(self, namespace: str, key: str) -> None:
        self.values.pop((namespace, key), None)


@pytest.fixture
def models() -> list[Contract]:
    items: list[Contract] = []

    def make[T: Contract](model_type: type[T], **data: object) -> T:
        instance = model_type.model_validate(data)
        items.append(instance)
        return instance

    area = make(
        a.Area,
        id=UUID(int=10),
        key="backend",
        name="Backend",
        description="Server systems",
        sort=0,
        archived_at=None,
    )
    predicate = make(a.VisibilityPredicate, q="languages", op="includes", value="python")
    any_condition = make(a.VisibilityAny, any=[predicate])
    not_condition = make(a.VisibilityNot, **{"not": predicate})
    all_condition = make(a.VisibilityAll, all=[any_condition, not_condition])
    section = make(a.QuizSection, key="general", position=0, visible_if=None)
    option = make(
        a.QuestionOption,
        id=UUID(int=11),
        key="python",
        label="Python",
        position=0,
        area_hints={"backend": 0.9},
    )
    question_data = dict(
        id=UUID(int=12),
        section="general",
        key="languages",
        text="Your language?",
        help=None,
        required=True,
        position=0,
        visible_if=all_condition,
        options=[option],
    )
    make(a.QuizQuestionBase, **question_data)
    configs = [
        (a.SingleChoiceQuestion, "single_choice", make(a.SingleChoiceConfig, allow_other=True)),
        (
            a.MultiChoiceQuestion,
            "multi_choice",
            make(a.MultiChoiceConfig, min=1, max=3, allow_other=True),
        ),
        (
            a.ScaleQuestion,
            "scale",
            make(a.ScaleConfig, min=1, max=10, min_label="New", max_label="Expert"),
        ),
        (a.TextQuestion, "text", make(a.TextConfig, max_length=1000, multiline=True)),
        (a.NumberQuestion, "number", make(a.NumberConfig, min=0, max=40, unit="hours")),
        (a.UrlQuestion, "url", make(a.UrlConfig)),
    ]
    questions = [
        make(model, **(question_data | {"key": kind, "type": kind, "config": config}))
        for model, kind, config in configs
    ]
    quiz = make(
        a.QuizDef,
        id=UUID(int=13),
        version=1,
        title="Member onboarding",
        status="published",
        published_at=NOW,
        sections=[section],
        questions=questions,
    )
    answer = make(a.OnboardingAnswer, value=["python"], other="Rust")
    answers = make(a.OnboardingAnswers, answers={"multi_choice": answer})
    repo = make(
        a.GithubRepo,
        name="hmm",
        description="Community platform",
        topics=["python"],
        language="Python",
        stars=12,
        pushed_at=NOW,
    )
    github = make(
        a.GithubSummary,
        login="member",
        account_created_at=NOW - timedelta(days=1000),
        languages={"Python": 80.0, "Rust": 20.0},
        repos=[repo],
        followers=15,
        contributions_last_year=320,
        public_repos=7,
        stars_total=42,
    )
    onboarding = make(
        a.OnboardingRequest,
        quiz=quiz,
        answers=answers,
        cv_text="Built Python services",
        github=github,
        areas=[area],
    )
    score = make(a.ScoreWithReason, value=7.5, reason=REASON)
    scores = make(a.OnboardingScores, area_scores={"backend": score}, discrepancies=[])
    effective = make(
        a.EffectiveScore, calculated=7.5, override=8, override_evidence="github_review", effective=8
    )
    reputation = make(a.RatingSummary, avg=9, count=3)
    offer = make(a.MentorOffer, area="backend", topics="Python services", active=True)
    member = make(
        a.MemberSnapshot,
        user_id=USER,
        display_name="Alex",
        about_md="Backend engineer",
        cv_text="Built Python services",
        github=github,
        area_scores={"backend": effective},
        scores_reviewed_at=NOW,
        xp=240,
        activity=18,
        hackathons_finished=2,
        times_led=1,
        projects_shipped=2,
        mentorships_completed=1,
        reputation={"lead": reputation},
        mentor_offers=[offer],
        mentor_capacity=3,
        mentor_active=1,
        joined_at=NOW - timedelta(days=400),
        mentor_notified_at=[NOW - timedelta(days=3)],
    )
    baseline = make(a.OnboardingBaseline, **scores.model_dump(), submitted_at=NOW)
    event_area = make(a.EventArea, area="backend", importance="primary", headcount=2)
    event = make(
        a.EventSnapshot,
        timezone="UTC",
        id=EVENT,
        title="Build weekend",
        summary="Ship a prototype",
        description_md="Build a community tool",
        themes=["community"],
        format="online",
        starts_at=NOW,
        ends_at=NOW + timedelta(days=2),
        areas=[event_area],
        team_size_min=2,
        team_size_max=5,
        difficulty=6,
        kind="hackathon",
        origin="manual",
        created_by=None,
    )
    recruiting = make(a.Recruiting, areas=["backend"], note="Need API experience")
    project = make(
        a.ProjectSnapshot,
        id=PROJECT,
        name="Mentor finder",
        goal="Find a mentor",
        description_md="Match learners with mentors",
        areas=["backend"],
        recruiting=recruiting,
        event_id=EVENT,
        status="active",
        team_status="active",
        links=["https://github.com/member/hmm"],
    )
    confirmation = make(
        a.ConfirmationIn,
        id=UUID(int=20),
        confirmer_id=OTHER,
        source="peer",
        confirmer_account_age=timedelta(days=300),
        confirmer_xp=180,
        confirmer_restrictions=[],
        ignored=False,
        flag_status=None,
        created_at=NOW,
        withdrawn_at=None,
    )
    result = make(
        a.ProjectResultContext,
        outcome="finished",
        place=2,
        award="Runner-up",
        place_status="verified",
        participants={USER: True, OTHER: False},
        member_areas={USER: "backend"},
        repo_url="https://github.com/member/hmm",
        demo_url=None,
        presentation_url=None,
        retrospective_md="Delivered the API",
        auto_closed=False,
        submitted_at=NOW,
    )
    contribution = make(a.ReleaseContribution, user_id=USER, contributed=True, area="backend")
    release = make(
        a.ReleaseContext,
        id=UUID(int=21),
        title="First release",
        notes_md="Public API",
        repo_url="https://github.com/member/hmm",
        demo_url=None,
        writeup_url=None,
        created_at=NOW,
        contributions=[contribution],
    )
    update = make(
        a.ProjectUpdateIn,
        id=UUID(int=22),
        author_id=USER,
        body_md="Implemented matching",
        links=[],
        created_at=NOW,
        deleted_at=None,
    )
    milestone = make(
        a.MilestoneIn,
        id=UUID(int=23),
        title="API",
        description=None,
        due_on=NOW.date(),
        done_by=USER,
        done_at=NOW,
    )
    checkin = make(
        a.CheckinIn,
        id=UUID(int=24),
        author_id=USER,
        note="Reviewed auth",
        occurred_on=NOW.date(),
        minutes=45,
    )
    effort = make(
        a.FactEffort, updates=[update], milestones=[milestone], checkins=[checkin], minutes=45
    )
    mentorship = make(
        a.MentorshipContext,
        id=UUID(int=25),
        kind="mentoring",
        status="closed",
        mentor_id=USER,
        learner_id=OTHER,
        area="backend",
        mentor_score_at_start=8,
        learner_score_at_start=3,
        learner_score_now=4,
        started_at=NOW - timedelta(days=7),
        closed_at=NOW,
        duration=timedelta(days=7),
        mentor_checkin_count=2,
        learner_checkin_count=3,
        earlier_pair_mentorships=0,
        outcome="reached",
        summary="Shipped authentication",
        mentor_acknowledged_at=NOW,
        endorsement="Good work",
    )
    context = make(
        a.FactContext,
        event=event,
        project=project,
        result=result,
        releases=[release],
        mentorship=None,
        attendance_status=None,
    )
    fact = make(
        a.Fact,
        type="project_member",
        id=UUID(int=26),
        role="lead",
        area="backend",
        trust="trusted",
        flag_status=None,
        excluded=False,
        proof_links=["https://github.com/member/hmm"],
        note="Implemented the matching API",
        created_at=NOW,
        started_at=NOW - timedelta(days=2),
        ended_at=NOW,
        confirmed_at=NOW,
        left_at=None,
        confirmations=[confirmation],
        counterparts=[OTHER],
        outcome="finished",
        effort=effort,
        context=context,
    )
    make(
        a.Fact,
        **(
            fact.model_dump()
            | {
                "type": "mentorship_side",
                "role": "mentor",
                "context": a.FactContext(
                    event=None,
                    project=None,
                    result=None,
                    releases=[],
                    mentorship=mentorship,
                    attendance_status=None,
                ),
                "outcome": "reached",
            }
        ),
    )
    make(
        a.Fact,
        **(
            fact.model_dump()
            | {
                "type": "attendance",
                "role": "speaker",
                "context": a.FactContext(
                    event=event,
                    project=None,
                    result=None,
                    releases=[],
                    mentorship=None,
                    attendance_status="attended",
                ),
                "outcome": None,
            }
        ),
    )
    entry = make(
        a.ActivityEntry,
        id=UUID(int=27),
        type="oss",
        title="Improve docs",
        description_md="Published an integration guide",
        links=["https://github.com/member/hmm"],
        area="backend",
        occurred_on=NOW.date(),
        created_at=NOW,
        deleted_at=None,
        flag_status="pardoned",
        excluded=False,
    )
    review_data = dict(
        id=UUID(int=28),
        reviewer_id=OTHER,
        reviewee_id=USER,
        context="lead",
        rating=9,
        subject_type="project",
        subject_id=PROJECT,
        created_at=NOW,
        revealed_at=NOW,
        removed_at=None,
        flag_status=None,
    )
    review = make(a.ReviewIn, **review_data, comment="Clear direction")
    pardoned = make(
        a.PardonedFlag, subject_type="activity_entry", subject_id=entry.id, reason="Duplicate text"
    )
    make(
        a.MemberHistory,
        **member.model_dump(),
        onboarding=baseline,
        facts=[fact],
        activity_entries=[entry],
        reviews_received=[review],
        xp_adjustments_total=-10,
        pardoned=[pardoned],
    )
    award = make(
        a.FactAward, fact_type="project_member", fact_id=fact.id, xp=40, activity=8, reason=REASON
    )
    flag = make(
        a.Flag,
        subject_type="ring",
        subject_id=USER,
        related_user_ids=[OTHER],
        reason="Repeated reciprocal confirmations",
        severity="medium",
    )
    make(
        a.MemberMetrics,
        area_scores={"backend": score},
        xp=40,
        activity=8,
        fact_awards=[award],
        flags=[flag],
    )
    fit = make(a.LeadFit, user_id=USER, fit=0.8, reason=REASON)
    team_member = make(
        a.TeamMemberSnapshot,
        **member.model_dump(),
        role="lead",
        area="backend",
        state="confirmed",
        willing_to_lead=True,
        claimed_lead=True,
        enrolled_at=NOW,
    )
    enrolled = make(
        a.EnrolledMember,
        **member.model_dump(),
        preferred_area="backend",
        willing_to_lead=True,
        enrolled_at=NOW,
        note="Can build APIs",
    )
    make(
        a.RecommendRequest,
        event=event,
        project=project,
        team=[team_member],
        enrolled=[enrolled],
        others=[member],
        limit_enrolled=5,
        limit_others=3,
    )
    rec = make(
        a.Rec,
        user_id=USER,
        score=0.9,
        reasons=[REASON],
        tags=["covers:backend"],
        suggested_area="backend",
    )
    make(a.Recommendations, enrolled=[rec], others=[])
    core = make(a.Core, project_id=PROJECT, lead=team_member, members=[])
    bounds = Bounds(teams=1, new_teams=0, placed=1, reserve=0, undersized_cores=0)
    make(
        a.FormationRequest,
        hackathon=event,
        kind="main",
        target=5,
        viable=4,
        max_size=5,
        bounds=bounds,
        cores=[core],
        unplaced=[enrolled],
        lead_fits={USER: fit},
    )
    planned = make(a.PlannedMember, user_id=USER, area="backend", reason=REASON)
    team = make(
        a.PlannedTeam, project_id=PROJECT, lead_user_id=None, members=[planned], reason=REASON
    )
    ranked = make(a.RankedMember, user_id=OTHER, rank=1, reason=REASON)
    merge = make(
        a.PlannedMerge, absorbed_team_id=UUID(int=30), target_team_id=PROJECT, reason=REASON
    )
    make(
        a.FormationPlan, teams=[team], reserve=[ranked], merges=[merge], summary="One balanced team"
    )
    composition = make(a.TeamComposition, project_id=PROJECT, members=[team_member], open_seats=3)
    make(a.ReserveRequest, hackathon=event, teams=[composition], reserve=[enrolled])
    make(
        a.MentorMatchRequest,
        area=area,
        topic="Python APIs",
        goal="Ship authentication",
        format="online",
        learner=member,
        candidates=[member],
        limit=5,
    )
    edge = make(
        a.ConfirmationEdge,
        id=confirmation.id,
        confirmer_id=OTHER,
        subject_user_id=USER,
        fact_type="project_member",
        fact_id=fact.id,
        context_id=PROJECT,
        source="peer",
        created_at=NOW,
        ignored=False,
        withdrawn_at=None,
        flag_status=None,
    )
    mentor_edge = make(
        a.MentorshipEdge,
        id=mentorship.id,
        mentor_id=USER,
        learner_id=OTHER,
        area="backend",
        kind="mentoring",
        outcome="reached",
        started_at=NOW,
        closed_at=NOW + timedelta(days=7),
    )
    review_edge = make(a.ReviewEdge, **review_data)
    member_event = make(
        a.MemberEventSummary,
        id=EVENT,
        creator_id=USER,
        created_at=NOW,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=2),
        attendees=[OTHER],
        confirmations=[edge],
    )
    account = make(
        a.AccountInfo,
        joined_at=NOW - timedelta(days=400),
        xp=240,
        github_account_age=timedelta(days=1000),
        restrictions=[],
    )
    make(
        a.CommunityGraph,
        confirmations=[edge],
        mentorships=[mentor_edge],
        reviews=[review_edge],
        member_events=[member_event],
        accounts={USER: account},
        pardoned=[pardoned],
    )
    make(a.AlgoSettings)
    make(a.LLMConfig, base_url="https://llm.example/v1", api_key="test-key", model="chat")
    assert onboarding.quiz is quiz
    return items


def test_all_models_round_trip(models: list[Contract]) -> None:
    expected = {
        value
        for value in vars(a).values()
        if isinstance(value, type)
        and issubclass(value, Contract)
        and value.__module__ == a.__name__
    }
    assert {type(model) for model in models} == expected
    for model in models:
        if isinstance(model, a.LLMConfig):
            data = model.model_dump(mode="json")
            data["api_key"] = model.api_key.get_secret_value()
            assert type(model).model_validate_json(json.dumps(data)) == model
        else:
            assert type(model).model_validate_json(model.model_dump_json(by_alias=True)) == model


def test_structural_algorithm(models: list[Contract]) -> None:
    def find[T: Contract](model: type[T]) -> T:
        return next(item for item in models if isinstance(item, model))

    class Implementation:
        version = "test-1"

        async def score_onboarding(
            self, ctx: a.AlgoContext, request: a.OnboardingRequest
        ) -> a.OnboardingScores:
            return find(a.OnboardingScores)

        async def compute_member(
            self, ctx: a.AlgoContext, member: a.MemberHistory, areas: list[a.Area]
        ) -> a.MemberMetrics:
            return find(a.MemberMetrics)

        async def lead_fit(
            self, ctx: a.AlgoContext, hackathon: a.EventSnapshot, candidates: list[a.MemberSnapshot]
        ) -> list[a.LeadFit]:
            return [find(a.LeadFit)]

        async def recommend(
            self, ctx: a.AlgoContext, request: a.RecommendRequest
        ) -> a.Recommendations:
            return find(a.Recommendations)

        async def form_teams(
            self, ctx: a.AlgoContext, request: a.FormationRequest
        ) -> a.FormationPlan:
            return find(a.FormationPlan)

        async def rank_reserve(
            self, ctx: a.AlgoContext, request: a.ReserveRequest
        ) -> list[a.RankedMember]:
            return [find(a.RankedMember)]

        async def match_mentors(
            self, ctx: a.AlgoContext, request: a.MentorMatchRequest
        ) -> list[a.Rec]:
            return [find(a.Rec)]

        async def scan_community(self, ctx: a.AlgoContext, graph: a.CommunityGraph) -> list[a.Flag]:
            return [find(a.Flag)]

    algorithm: a.Algorithm = Implementation()
    assert algorithm.version == "test-1"


@pytest.mark.parametrize(
    ("model_type", "field"),
    [
        (a.AlgoSettings, "team_size_default_min"),
        (a.AlgoSettings, "team_size_default_max"),
        (a.EventSnapshot, "team_size_min"),
        (a.EventSnapshot, "team_size_max"),
        (a.FormationRequest, "target"),
        (a.FormationRequest, "viable"),
        (a.FormationRequest, "max_size"),
    ],
)
def test_team_sizes_have_no_upper_bound(
    models: list[Contract], model_type: type[Contract], field: str
) -> None:
    original = next(model for model in models if type(model) is model_type)
    data = original.model_dump(mode="json")
    for size in (1, 21, 1000):
        restored = model_type.model_validate_json(json.dumps(data | {field: size}))
        assert getattr(restored, field) == size
    with pytest.raises(ValidationError):
        model_type.model_validate(data | {field: 0})


def test_event_metadata_can_be_null(models: list[Contract]) -> None:
    event = next(model for model in models if isinstance(model, a.EventSnapshot))
    imported = a.EventSnapshot.model_validate(
        event.model_dump() | {"summary": None, "description_md": None, "format": None}
    )
    assert imported.summary is None
    assert imported.description_md is None
    assert imported.format is None
    assert a.EventSnapshot.model_validate_json(imported.model_dump_json()) == imported


@pytest.mark.parametrize("difficulty", [0, 0.5, 11, float("nan"), float("inf")])
def test_invalid_event_difficulty(models: list[Contract], difficulty: float) -> None:
    event = next(model for model in models if isinstance(model, a.EventSnapshot))
    with pytest.raises(ValidationError):
        a.EventSnapshot.model_validate(event.model_dump() | {"difficulty": difficulty})


@pytest.mark.parametrize("difficulty", [1, 10, None])
def test_valid_event_difficulty(models: list[Contract], difficulty: float | None) -> None:
    event = next(model for model in models if isinstance(model, a.EventSnapshot))
    restored = a.EventSnapshot.model_validate(event.model_dump() | {"difficulty": difficulty})
    assert restored.difficulty == difficulty


def test_raw_reviews_only_belong_to_history(models: list[Contract]) -> None:
    member = next(model for model in models if type(model) is a.MemberSnapshot)
    history = next(model for model in models if isinstance(model, a.MemberHistory))
    assert "reviews_received" not in a.MemberSnapshot.model_fields
    assert history.reviews_received
    with pytest.raises(ValidationError):
        a.MemberSnapshot.model_validate(member.model_dump() | {"reviews_received": []})
    data = history.model_dump()
    del data["reviews_received"]
    with pytest.raises(ValidationError):
        a.MemberHistory.model_validate(data)


def test_mentor_notification_timestamps(models: list[Contract]) -> None:
    member = next(model for model in models if type(model) is a.MemberSnapshot)
    assert member.mentor_notified_at == [NOW - timedelta(days=3)]
    data = member.model_dump()
    del data["mentor_notified_at"]
    restored = a.MemberSnapshot.model_validate(data)
    assert restored.mentor_notified_at == []
    assert a.MemberSnapshot.model_validate_json(member.model_dump_json()) == member
    with pytest.raises(ValidationError):
        a.MemberSnapshot.model_validate(data | {"mentor_notifications": []})


@pytest.mark.parametrize("value", [-1, 11, float("nan"), float("inf")])
def test_invalid_area_score(value: float) -> None:
    with pytest.raises(ValidationError):
        a.ScoreWithReason(value=value, reason=REASON)


@pytest.mark.parametrize("value", [-0.1, 1.5, float("nan"), float("inf")])
def test_invalid_fit_and_recommendation(value: float) -> None:
    with pytest.raises(ValidationError):
        a.LeadFit(user_id=USER, fit=value, reason=REASON)
    with pytest.raises(ValidationError):
        a.Rec(user_id=USER, score=value, reasons=[REASON], tags=[], suggested_area=None)


@pytest.mark.parametrize("reasons", [[], [REASON] * 4, ["x" * 201], [""], ["   "]])
def test_invalid_reasons(reasons: list[str]) -> None:
    with pytest.raises(ValidationError):
        a.Rec(user_id=USER, score=0.5, reasons=reasons, tags=[], suggested_area=None)


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201])
def test_every_singular_reason_is_bounded(models: list[Contract], reason: str) -> None:
    for model in models:
        if "reason" in type(model).model_fields:
            with pytest.raises(ValidationError):
                type(model).model_validate(model.model_dump(by_alias=True) | {"reason": reason})


def test_context_is_immutable() -> None:
    cache: a.AlgoCache = MemoryCache()
    context = a.AlgoContext(
        now=NOW,
        settings=a.AlgoSettings(),
        llm=None,
        cache=cache,
        log=logging.getLogger("algo-test"),
    )
    attribute = "now"
    with pytest.raises(FrozenInstanceError):
        setattr(context, attribute, NOW + timedelta(days=1))
    assert not hasattr(context, "__dict__")
    assert get_type_hints(a.AlgoContext)["now"] is datetime


def test_contracts_forbid_unknown_fields_and_are_frozen() -> None:
    score = a.ScoreWithReason(value=8, reason=REASON)
    with pytest.raises(ValidationError):
        a.ScoreWithReason.model_validate({"value": 8, "reason": REASON, "unknown": True})
    attribute = "value"
    with pytest.raises(ValidationError):
        setattr(score, attribute, 9)
    assert a.FactAward(fact_type=FactType.ATTENDANCE, fact_id=USER, xp=0, activity=1, reason=REASON)
