import logging
import socket
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fixtures import (
    CommunityCase,
    FormationCase,
    MentorshipCase,
    load_community_cases,
    load_events,
    load_formation_cases,
    load_members,
    load_mentorship_cases,
)

from hmm.algo import models as a
from hmm.algo.fake import FakeAlgorithm
from hmm.algo.models import AlgoContext, Algorithm, AlgoSettings
from hmm.algo.pack import Unit, pack
from hmm.common import SEED_AREA_KEYS, Contract

IMPLEMENTATIONS: list[type[Algorithm]] = [FakeAlgorithm]


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    async def get(self, namespace: str, key: str) -> bytes | None:
        return self.values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: bytes, ttl: timedelta | None) -> None:
        self.values[namespace, key] = value

    async def delete(self, namespace: str, key: str) -> None:
        self.values.pop((namespace, key), None)


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda implementation: implementation.__name__)
def algorithm(request: pytest.FixtureRequest) -> Algorithm:
    implementation: type[Algorithm] = request.param
    return implementation()


@pytest.fixture
def ctx() -> AlgoContext:
    return AlgoContext(
        now=datetime(2026, 11, 12, 10, tzinfo=UTC),
        settings=AlgoSettings(),
        llm=None,
        cache=MemoryCache(),
        log=logging.getLogger("algo.conformance"),
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("Algorithm conformance must work without network or LLM access")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


CASES = load_formation_cases()


@pytest.mark.parametrize("case", CASES.values(), ids=CASES)
async def test_conformance_formation(
    algorithm: a.Algorithm, ctx: a.AlgoContext, case: FormationCase
) -> None:
    request = case.request
    original = request.model_dump_json()
    plan = await algorithm.form_teams(ctx, request)
    assert plan == await algorithm.form_teams(ctx, request)
    assert request.model_dump_json() == original
    assert a.FormationPlan.model_validate_json(plan.model_dump_json()) == plan
    units = {unit.id: unit for unit in request.units}
    placed = [unit_id for team in plan.teams for unit_id in team.units]
    assert set(placed) <= set(units)
    assert all(count == 1 for count in Counter(placed).values())
    sizes = [sum(len(units[unit_id].members) for unit_id in team.units) for team in plan.teams]
    assert all(request.min_size <= size <= request.max_size for size in sizes)
    baseline = pack(
        [
            Unit(
                id=unit.id,
                size=len(unit.members),
                queued_at=min(member.enrolled_at for member in unit.members),
            )
            for unit in request.units
        ],
        [],
        request.target,
        request.min_size,
        request.max_size,
    )
    assert sum(sizes) >= sum(
        len(units[unit_id].members) for team in baseline.new_teams for unit_id in team
    )
    assert sorted(sizes) == sorted(case.expect.team_sizes)
    assert (
        sum(len(unit.members) for unit in request.units) - sum(sizes) == case.expect.queued_people
    )
    assert plan.summary.strip()
    assert all(team.reason.strip() for team in plan.teams)


MEMBERS = load_members()
EVENTS = load_events()
AREAS = [
    a.Area(id=UUID(int=index), key=key, name=key, description=key, sort=index, archived_at=None)
    for index, key in enumerate(SEED_AREA_KEYS, 1)
]


def assert_valid(value: Contract) -> None:
    assert type(value).model_validate_json(value.model_dump_json()) == value


def assert_scores(scores: dict[str, a.ScoreWithReason]) -> None:
    assert set(scores) == set(SEED_AREA_KEYS)
    for score in scores.values():
        assert 0 <= score.value <= 10
        assert score.reason.strip()
        assert len(score.reason) <= 200


@pytest.mark.parametrize("member", MEMBERS.values(), ids=MEMBERS)
async def test_conformance_onboarding(
    algorithm: a.Algorithm, ctx: a.AlgoContext, member: a.MemberHistory
) -> None:
    options = [
        a.QuestionOption(
            id=area.id,
            key=area.key,
            label=area.name,
            position=area.sort,
            area_hints={area.key: 0.5},
        )
        for area in AREAS
    ]
    quiz = a.QuizDef(
        id=UUID(int=100),
        version=1,
        title="Conformance quiz",
        status="published",
        published_at=ctx.now,
        sections=[a.QuizSection(key="skills", position=0, visible_if=None)],
        questions=[
            a.MultiChoiceQuestion(
                id=UUID(int=101),
                section="skills",
                key="areas",
                text="Select areas",
                help=None,
                required=True,
                position=0,
                visible_if=None,
                options=options,
                type=a.QuestionType.MULTI_CHOICE,
                config=a.MultiChoiceConfig(min=0, max=len(options), allow_other=False),
            )
        ],
    )
    request = a.OnboardingRequest(
        quiz=quiz,
        answers=a.OnboardingAnswers(
            answers={"areas": a.OnboardingAnswer(value=list(member.area_scores))}
        ),
        cv_text=member.cv_text,
        github=member.github,
        areas=AREAS,
    )
    original = request.model_dump_json()
    result = await algorithm.score_onboarding(ctx, request)
    assert result == await algorithm.score_onboarding(ctx, request)
    assert request.model_dump_json() == original
    assert_valid(result)
    assert_scores(result.area_scores)


@pytest.mark.parametrize("member", MEMBERS.values(), ids=MEMBERS)
async def test_conformance_member_metrics(
    algorithm: a.Algorithm, ctx: a.AlgoContext, member: a.MemberHistory
) -> None:
    original = member.model_dump_json()
    result = await algorithm.compute_member(ctx, member, AREAS)
    assert result == await algorithm.compute_member(ctx, member, AREAS)
    assert member.model_dump_json() == original
    assert_valid(result)
    assert_scores(result.area_scores)
    assert result.xp >= 0
    assert result.activity >= 0
    facts = {(fact.type, fact.id): fact for fact in member.facts}
    assert {(award.fact_type, award.fact_id) for award in result.fact_awards} <= set(facts)
    for award in result.fact_awards:
        assert award.reason.strip()
        assert award.xp >= 0 and award.activity >= 0
        fact = facts[award.fact_type, award.fact_id]
        if fact.excluded or fact.flag_status == "to_check":
            assert award.xp == award.activity == 0
    without_reviews = member.model_copy(update={"reviews_received": [], "reputation": {}})
    assert (await algorithm.compute_member(ctx, without_reviews, AREAS)).xp == result.xp
    changed = member.model_copy(update={"xp": 99999.0, "activity": 99999.0, "area_scores": {}})
    recalculated = await algorithm.compute_member(ctx, changed, AREAS)
    assert recalculated.area_scores == result.area_scores
    assert recalculated.xp == result.xp
    assert recalculated.activity == result.activity
    subjects = {fact.id for fact in member.facts} | {entry.id for entry in member.activity_entries}
    subjects |= {review.id for review in member.reviews_received} | {member.user_id}
    subjects |= {item.id for fact in member.facts for item in fact.confirmations}
    assert all(flag.subject_id in subjects for flag in result.flags)


@pytest.mark.parametrize("name", load_formation_cases())
async def test_conformance_recommendations(
    algorithm: a.Algorithm, ctx: a.AlgoContext, name: str
) -> None:
    formation = load_formation_cases()[name].request
    project = a.ProjectSnapshot(
        id=UUID(int=200),
        name="Conformance project",
        goal="Build a prototype",
        description_md="Team development",
        areas=[area.area for area in formation.hackathon.areas],
        recruiting=None,
        event_id=formation.hackathon.id,
        status="active",
        team_status="formed",
        links=[],
    )
    request = a.RecommendRequest(
        event=formation.hackathon,
        project=project,
        team=[],
        enrolled=[member for unit in formation.units for member in unit.members],
        others=list(MEMBERS.values())[-10:],
        limit_enrolled=3,
        limit_others=2,
    )
    original = request.model_dump_json()
    result = await algorithm.recommend(ctx, request)
    assert result == await algorithm.recommend(ctx, request)
    assert request.model_dump_json() == original
    assert_valid(result)
    for recs, candidates, limit in (
        (result.enrolled, request.enrolled, request.limit_enrolled),
        (result.others, request.others, request.limit_others),
    ):
        assert len(recs) <= limit
        assert {rec.user_id for rec in recs} <= {member.user_id for member in candidates}
        assert len(recs) == len({rec.user_id for rec in recs})
        for rec in recs:
            assert 0 <= rec.score <= 1
            assert 1 <= len(rec.reasons) <= 3
            assert all(reason.strip() and len(reason) <= 200 for reason in rec.reasons)


MENTORSHIP = load_mentorship_cases()
COMMUNITY = load_community_cases()


@pytest.mark.parametrize("case", MENTORSHIP.values(), ids=MENTORSHIP)
async def test_conformance_mentorship(
    algorithm: a.Algorithm, ctx: a.AlgoContext, case: MentorshipCase
) -> None:
    request = case.request
    original = request.model_dump_json()
    result = await algorithm.match_mentors(ctx, request)
    assert result == await algorithm.match_mentors(ctx, request)
    assert request.model_dump_json() == original
    candidates = {member.user_id: member for member in request.candidates}
    users = {rec.user_id for rec in result}
    assert len(result) <= request.limit
    assert len(result) == len(users)
    assert users <= set(candidates)
    assert users.isdisjoint(case.expect.must_not_appear)
    assert users.isdisjoint(exclusion.member.user_id for exclusion in case.excluded_candidates)
    assert request.learner.user_id not in users
    for rec in result:
        assert a.Rec.model_validate_json(rec.model_dump_json()) == rec
        member = candidates[rec.user_id]
        assert member.mentor_active < member.mentor_capacity
        assert 0 <= rec.score <= 1
        assert 1 <= len(rec.reasons) <= 3
        assert all(reason.strip() and len(reason) <= 200 for reason in rec.reasons)
    for limit in (0, 1):
        limited = request.model_copy(update={"limit": limit})
        assert len(await algorithm.match_mentors(ctx, limited)) <= limit


@pytest.mark.parametrize("case", COMMUNITY.values(), ids=COMMUNITY)
async def test_conformance_community(
    algorithm: a.Algorithm, ctx: a.AlgoContext, case: CommunityCase
) -> None:
    graph = case.graph
    original = graph.model_dump_json()
    result = await algorithm.scan_community(ctx, graph)
    assert result == await algorithm.scan_community(ctx, graph)
    assert graph.model_dump_json() == original
    subjects = {("confirmation", edge.id) for edge in graph.confirmations}
    subjects |= {("mentorship", edge.id) for edge in graph.mentorships}
    subjects |= {("review", edge.id) for edge in graph.reviews}
    subjects |= {("attendance", event.id) for event in graph.member_events}
    subjects |= {("profile", user_id) for user_id in graph.accounts}
    subjects |= {(flag.subject_type, flag.subject_id) for flag in graph.pardoned}
    pardoned = {(flag.subject_type, flag.subject_id, flag.reason) for flag in graph.pardoned}
    related = set()
    for flag in result:
        assert a.Flag.model_validate_json(flag.model_dump_json()) == flag
        assert (flag.subject_type, flag.subject_id) in subjects
        assert set(flag.related_user_ids) <= set(graph.accounts)
        assert (flag.subject_type, flag.subject_id, flag.reason) not in pardoned
        assert flag.reason.strip()
        related.update(flag.related_user_ids)
    assert related.isdisjoint(case.expect_not_flagged)


@pytest.mark.parametrize(
    "name", ["mutual_confirmation_ring", "rating_ring", "reciprocal_mentoring", "scenario_16"]
)
async def test_conformance_planted_rings(
    algorithm: a.Algorithm, ctx: a.AlgoContext, name: str
) -> None:
    case = COMMUNITY[name]
    result = await algorithm.scan_community(ctx, case.graph)
    for users in case.expect_flagged:
        assert any(users <= set(flag.related_user_ids) for flag in result)
