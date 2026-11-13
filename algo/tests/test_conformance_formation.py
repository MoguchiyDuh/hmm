from collections import Counter

import pytest
from hmm_contracts import algo as a
from hmm_contracts.fixtures import FormationCase, load_formation_cases
from hmm_contracts.formation import placement_bounds

CASES = load_formation_cases()


def assert_plan(ctx: a.AlgoContext, request: a.FormationRequest, plan: a.FormationPlan) -> None:
    sizes = [1 + len(core.members) for core in request.cores]
    bounds = placement_bounds(
        len(request.unplaced), sizes, request.viable, request.target, request.max_size
    )
    assert request.bounds == bounds
    assert a.FormationPlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.summary.strip()
    expected = [member.user_id for member in request.unplaced]
    expected += [member.user_id for core in request.cores for member in [core.lead, *core.members]]
    actual = [member.user_id for team in plan.teams for member in team.members]
    actual += [member.user_id for member in plan.reserve]
    assert Counter(actual) == Counter(expected)
    assert len(actual) == len(set(actual))
    assert len(plan.teams) == bounds.teams
    assert sum(team.project_id is None for team in plan.teams) == bounds.new_teams
    assert sum(len(team.members) for team in plan.teams) == sum(sizes) + bounds.placed
    assert len(plan.reserve) == bounds.reserve
    assert [member.rank for member in plan.reserve] == list(range(1, bounds.reserve + 1))
    cores = {core.project_id: core for core in request.cores}
    existing = {team.project_id: team for team in plan.teams if team.project_id is not None}
    assert set(existing) == set(cores)
    unplaced = {member.user_id: member for member in request.unplaced}
    undersized = 0
    for team in plan.teams:
        assert team.reason.strip()
        assert 1 <= len(team.members) <= request.max_size
        assert all(member.reason.strip() for member in team.members)
        if team.project_id is not None:
            core = cores[team.project_id]
            assert team.lead_user_id in (None, core.lead.user_id)
            members = {member.user_id: member for member in team.members}
            for member in [core.lead, *core.members]:
                assert member.user_id in members
                assert members[member.user_id].area == member.area
            if len(team.members) < request.viable:
                undersized += 1
                targets = [
                    other
                    for other in plan.teams
                    if other is not team
                    and len(other.members) + len(team.members) <= request.max_size
                ]
                if targets:
                    assert any(
                        merge.absorbed_team_id == team.project_id
                        or merge.target_team_id == team.project_id
                        for merge in plan.merges
                    )
        else:
            assert len(team.members) >= request.viable
            members = [unplaced[member.user_id] for member in team.members]
            assert team.lead_user_id in {member.user_id for member in members}

            def order(member: a.EnrolledMember) -> tuple[int, float]:
                fit = request.lead_fits.get(member.user_id)
                value = fit.fit if fit is not None else 0.0
                tier = 4
                if member.willing_to_lead:
                    tier = 2 if value >= ctx.settings.lead_fit_threshold else 3
                return tier, -value

            lead = next(member for member in members if member.user_id == team.lead_user_id)
            assert order(lead) == min(order(member) for member in members)
    assert undersized == bounds.undersized_cores
    for merge in plan.merges:
        assert merge.absorbed_team_id in existing
        assert merge.target_team_id in existing
        assert merge.absorbed_team_id != merge.target_team_id
        assert (
            len(existing[merge.absorbed_team_id].members)
            + len(existing[merge.target_team_id].members)
            <= request.max_size
        )
        assert merge.reason.strip()
    assert all(member.reason.strip() for member in plan.reserve)


@pytest.mark.parametrize("case", CASES.values(), ids=CASES)
async def test_conformance_formation(
    algorithm: a.Algorithm, ctx: a.AlgoContext, case: FormationCase
) -> None:
    request = case.request
    original = request.model_dump_json()
    plan = await algorithm.form_teams(ctx, request)
    assert plan == await algorithm.form_teams(ctx, request)
    assert request.model_dump_json() == original
    assert_plan(ctx, request, plan)
    for project_id, users in case.expect.fixed_cores.items():
        team = next(team for team in plan.teams if team.project_id == project_id)
        assert set(users) <= {member.user_id for member in team.members}
    if case.expect.merge_proposal_required:
        assert plan.merges


@pytest.mark.parametrize("free", [0, 1, 2, 3, 5, 8, 13, 20, 60])
@pytest.mark.parametrize("core_count", [0, 1, 2])
async def test_conformance_formation_boundaries(
    algorithm: a.Algorithm, ctx: a.AlgoContext, free: int, core_count: int
) -> None:
    source = CASES["scenario_03_04"].request
    pool = CASES["stress_100"].request.unplaced
    cores = source.cores[:core_count]
    core_users = {member.user_id for core in cores for member in [core.lead, *core.members]}
    unplaced = [member for member in pool if member.user_id not in core_users][:free]
    request = source.model_copy(
        update={
            "cores": cores,
            "unplaced": unplaced,
            "bounds": placement_bounds(
                free,
                [1 + len(core.members) for core in cores],
                source.viable,
                source.target,
                source.max_size,
            ),
        }
    )
    assert_plan(ctx, request, await algorithm.form_teams(ctx, request))


@pytest.mark.parametrize("case", CASES.values(), ids=CASES)
async def test_conformance_reserve(
    algorithm: a.Algorithm, ctx: a.AlgoContext, case: FormationCase
) -> None:
    request = a.ReserveRequest(
        hackathon=case.request.hackathon, teams=[], reserve=case.request.unplaced
    )
    result = await algorithm.rank_reserve(ctx, request)
    assert result == await algorithm.rank_reserve(ctx, request)
    assert Counter(item.user_id for item in result) == Counter(
        member.user_id for member in request.reserve
    )
    assert sorted(item.rank for item in result) == list(range(1, len(request.reserve) + 1))
    for item in result:
        assert a.RankedMember.model_validate_json(item.model_dump_json()) == item
        assert item.reason.strip()
