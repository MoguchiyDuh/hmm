from collections import Counter

import pytest
from hmm_contracts import algo as a
from hmm_contracts.fixtures import FormationCase, load_formation_cases
from hmm_contracts.formation import Unit, pack

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
