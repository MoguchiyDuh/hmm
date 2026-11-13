import pytest
from hmm_contracts import algo as a
from hmm_contracts.fixtures import (
    CommunityCase,
    MentorshipCase,
    load_community_cases,
    load_mentorship_cases,
)

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
