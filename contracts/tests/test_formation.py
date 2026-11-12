from functools import cache
from itertools import product

import pytest
from hmm_contracts.formation import Bounds, placement_bounds
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("free", "cores", "expected"),
    [
        (6, [], Bounds(teams=1, new_teams=1, placed=6, reserve=0, undersized_cores=0)),
        (8, [], Bounds(teams=1, new_teams=1, placed=6, reserve=2, undersized_cores=0)),
        (10, [], Bounds(teams=2, new_teams=2, placed=10, reserve=0, undersized_cores=0)),
        (11, [], Bounds(teams=2, new_teams=2, placed=11, reserve=0, undersized_cores=0)),
        (13, [], Bounds(teams=2, new_teams=2, placed=12, reserve=1, undersized_cores=0)),
        (15, [], Bounds(teams=3, new_teams=3, placed=15, reserve=0, undersized_cores=0)),
        (10, [2, 2], Bounds(teams=2, new_teams=0, placed=8, reserve=2, undersized_cores=0)),
    ],
)
def test_documented_examples(free: int, cores: list[int], expected: Bounds) -> None:
    assert placement_bounds(free, cores, 5, 6, 6) == expected


@pytest.mark.parametrize(
    ("free", "cores", "expected"),
    [
        (0, [], Bounds(teams=0, new_teams=0, placed=0, reserve=0, undersized_cores=0)),
        (0, [2, 5, 6], Bounds(teams=3, new_teams=0, placed=0, reserve=0, undersized_cores=1)),
        (1, [1, 4, 3], Bounds(teams=3, new_teams=0, placed=1, reserve=0, undersized_cores=2)),
        (2, [1, 4, 3], Bounds(teams=3, new_teams=0, placed=2, reserve=0, undersized_cores=2)),
        (3, [1, 4, 3], Bounds(teams=3, new_teams=0, placed=3, reserve=0, undersized_cores=1)),
        (7, [1, 4, 3], Bounds(teams=3, new_teams=0, placed=7, reserve=0, undersized_cores=0)),
        (1, [1, 5], Bounds(teams=2, new_teams=0, placed=1, reserve=0, undersized_cores=1)),
        (2, [5, 5], Bounds(teams=2, new_teams=0, placed=2, reserve=0, undersized_cores=0)),
        (5, [5], Bounds(teams=2, new_teams=1, placed=5, reserve=0, undersized_cores=0)),
        (4, [6], Bounds(teams=1, new_teams=0, placed=0, reserve=4, undersized_cores=0)),
        (5, [6], Bounds(teams=2, new_teams=1, placed=5, reserve=0, undersized_cores=0)),
    ],
)
def test_edges(free: int, cores: list[int], expected: Bounds) -> None:
    assert placement_bounds(free, cores, 5, 6, 6) == expected


@pytest.mark.parametrize(
    ("free", "cores", "v", "t", "max_size", "message"),
    [
        (-1, [], 5, 6, 6, "free must be nonnegative"),
        (0, [], 0, 6, 6, "v must be at least 1"),
        (0, [], -1, 6, 6, "v must be at least 1"),
        (0, [], 7, 6, 6, "v must not exceed max_size"),
        (0, [], 1, 1, 0, "v must not exceed max_size"),
        (0, [], 1, 1, -1, "v must not exceed max_size"),
        (0, [], 5, 4, 6, "t must be between v and max_size"),
        (0, [], 5, 7, 6, "t must be between v and max_size"),
        (0, [0], 5, 6, 6, "core size at index 0 must be between 1 and max_size"),
        (0, [2, -1], 5, 6, 6, "core size at index 1 must be between 1 and max_size"),
        (0, [7], 5, 6, 6, "core size at index 0 must be between 1 and max_size"),
    ],
)
def test_invalid_inputs(
    free: int, cores: list[int], v: int, t: int, max_size: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        placement_bounds(free, cores, v, t, max_size)


@cache
def new_team_placements(budget: int, minimum: int, maximum: int) -> tuple[tuple[int, ...], ...]:
    placements: list[tuple[int, ...]] = [()]
    for size in range(minimum, min(budget, maximum) + 1):
        for tail in new_team_placements(budget - size, size, maximum):
            placements.append((size, *tail))
    return tuple(placements)


def brute_force_bounds(free: int, cores: list[int], v: int, max_size: int) -> Bounds:
    best: tuple[int, int, int] | None = None
    shortage = free <= sum(v - size for size in cores if size < v)
    priority = sorted(
        (index for index, size in enumerate(cores) if size < v), key=cores.__getitem__, reverse=True
    )
    for additions in product(*(range(min(free, max_size - size) + 1) for size in cores)):
        spent = sum(additions)
        if spent > free:
            continue
        sizes = [size + addition for size, addition in zip(cores, additions, strict=True)]
        undersized = sum(size < v for size in sizes)
        if shortage:
            if any(
                addition > max(0, v - size) for size, addition in zip(cores, additions, strict=True)
            ):
                continue
            if any(
                additions[later] > 0 and sizes[earlier] < v
                for offset, earlier in enumerate(priority)
                for later in priority[offset + 1 :]
            ):
                continue
        elif undersized:
            continue
        for new_sizes in new_team_placements(free - spent, v, max_size):
            if new_sizes and undersized:
                continue
            candidate = (spent + sum(new_sizes), -len(new_sizes), -undersized)
            if best is None or candidate > best:
                best = candidate
    assert best is not None
    placed, negative_new_teams, negative_undersized = best
    return Bounds(
        teams=len(cores) - negative_new_teams,
        new_teams=-negative_new_teams,
        placed=placed,
        reserve=free - placed,
        undersized_cores=-negative_undersized,
    )


@st.composite
def formation_inputs(draw: st.DrawFn) -> tuple[int, list[int], int, int, int]:
    max_size = draw(st.integers(min_value=1, max_value=7))
    v = draw(st.integers(min_value=1, max_value=max_size))
    t = draw(st.integers(min_value=v, max_value=max_size))
    free = draw(st.integers(min_value=0, max_value=20))
    cores = draw(st.lists(st.integers(min_value=1, max_value=max_size), max_size=5))
    return free, cores, v, t, max_size


@given(formation_inputs())
@settings(max_examples=600, deadline=None)
def test_matches_brute_force(inputs: tuple[int, list[int], int, int, int]) -> None:
    free, cores, v, t, max_size = inputs
    original_cores = cores.copy()
    actual = placement_bounds(free, cores, v, t, max_size)
    assert actual == brute_force_bounds(free, cores, v, max_size)
    assert actual.placed + actual.reserve == free
    assert actual.teams == len(cores) + actual.new_teams
    assert actual.new_teams == 0 or actual.undersized_cores == 0
    assert placement_bounds(free, cores, v, t, max_size) == actual
    assert placement_bounds(free, cores, v, v, max_size) == actual
    assert placement_bounds(free, cores, v, max_size, max_size) == actual
    assert placement_bounds(free, list(reversed(cores)), v, t, max_size) == actual
    assert cores == original_cores
