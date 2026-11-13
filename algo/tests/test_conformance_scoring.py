from uuid import UUID

import pytest
from hmm_contracts import algo as a
from hmm_contracts.common import SEED_AREA_KEYS, Contract
from hmm_contracts.fixtures import load_events, load_formation_cases, load_members

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


@pytest.mark.parametrize("event", EVENTS.values(), ids=EVENTS)
async def test_conformance_lead_fits(
    algorithm: a.Algorithm, ctx: a.AlgoContext, event: a.EventSnapshot
) -> None:
    candidates: list[a.MemberSnapshot] = list(MEMBERS.values())
    result = await algorithm.lead_fit(ctx, event, candidates)
    assert result == await algorithm.lead_fit(ctx, event, candidates)
    assert {fit.user_id for fit in result} <= {member.user_id for member in candidates}
    assert len(result) == len({fit.user_id for fit in result})
    for fit in result:
        assert_valid(fit)
        assert 0 <= fit.fit <= 1
        assert fit.reason.strip()


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
        team_status="forming",
        links=[],
    )
    request = a.RecommendRequest(
        event=formation.hackathon,
        project=project,
        team=[member for core in formation.cores for member in [core.lead, *core.members]],
        enrolled=formation.unplaced,
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
