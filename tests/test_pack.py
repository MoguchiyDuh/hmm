from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from random import Random
from uuid import UUID

import pytest
from pydantic import ValidationError

from hmm.algo.pack import OpenTeam, Packing, Unit, pack

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def make_units(sizes: list[int]) -> list[Unit]:
    return [
        Unit(id=UUID(int=index + 1), size=size, queued_at=NOW + timedelta(seconds=index))
        for index, size in enumerate(sizes)
    ]


@pytest.mark.parametrize(
    ("sizes", "minimum", "maximum", "expected", "queued"),
    [
        ([1] * 7, 3, 5, [4, 3], 0),
        ([1] * 8, 3, 5, [4, 4], 0),
        ([1] * 11, 3, 5, [4, 4, 3], 0),
        ([1] * 2, 3, 5, [], 2),
        ([3, 3], 3, 5, [3, 3], 0),
        ([3, 2], 3, 5, [5], 0),
        ([4, 1, 1, 1, 1], 3, 5, [4, 4], 0),
        ([4, 3, 1], 3, 5, [4, 4], 0),
        ([2, 2, 2], 3, 5, [4], 2),
        ([3, 3, 2, 2, 2], 3, 6, [6, 6], 0),
        ([1] * 7, 4, 5, [5], 2),
    ],
)
def test_spec_examples(
    sizes: list[int], minimum: int, maximum: int, expected: list[int], queued: int
) -> None:
    units = make_units(sizes)
    by_id = {unit.id: unit.size for unit in units}
    result = pack(units, [], maximum, minimum, maximum)
    assert (
        sorted((sum(by_id[unit_id] for unit_id in team) for team in result.new_teams), reverse=True)
        == expected
    )
    assert sum(by_id[unit_id] for unit_id in result.queue) == queued
    assert result.fills == {}


def test_fill_order_bans_and_room() -> None:
    units = make_units([3, 2, 1, 1])
    first = OpenTeam(id=UUID(int=101), size=3, banned_unit_ids=frozenset({units[1].id}))
    second = OpenTeam(id=UUID(int=102), size=2)
    result = pack(list(reversed(units)), [first, second], 4, 3, 5)
    assert result.fills == {first.id: [units[2].id], second.id: [units[1].id]}
    assert result.queue == []
    assert result.new_teams == [[units[0].id, units[3].id]]


def test_queue_ties_use_id() -> None:
    units = [Unit(id=UUID(int=index), size=1, queued_at=NOW) for index in (3, 2, 1)]
    assert pack(units, [], 5, 4, 5).queue == [UUID(int=index) for index in (1, 2, 3)]


def test_round_uses_ties_to_even() -> None:
    units = make_units([1] * 10)
    assert len(pack(units, [], 4, 3, 5).new_teams) == 2


def test_fullest_fit_places_more_people() -> None:
    units = make_units([3, 3, 2, 2, 2])
    result = pack(units, [], 6, 3, 6)
    assert result.new_teams == [[units[0].id, units[1].id], [units[2].id, units[3].id, units[4].id]]
    assert result.queue == []


def test_equal_placement_keeps_smallest_fit() -> None:
    units = make_units([1, 1, 1, 1, 1, 2])
    result = pack(units, [], 3, 3, 3)
    assert result.new_teams == [
        [units[5].id, units[2].id],
        [units[0].id, units[1].id, units[3].id],
    ]
    assert result.queue == [units[4].id]


def test_open_teams_at_or_above_target_are_not_filled() -> None:
    units = make_units([1])
    teams = [OpenTeam(id=UUID(int=101), size=4), OpenTeam(id=UUID(int=102), size=5)]
    result = pack(units, teams, 4, 3, 5)
    assert result.fills == {}
    assert result.queue == [units[0].id]


def test_random_packing_invariants() -> None:
    random = Random(12)
    for _ in range(5000):
        maximum = random.randint(1, 10)
        minimum = random.randint(1, maximum)
        target = random.randint(minimum, maximum)
        sizes: list[int] = []
        room = random.randint(0, 40)
        while room:
            size = random.randint(1, min(room, maximum))
            sizes.append(size)
            room -= size
        units = make_units(sizes)
        teams = [
            OpenTeam(
                id=UUID(int=101 + index),
                size=random.randint(0, maximum),
                banned_unit_ids=frozenset(
                    unit.id for unit in units if random.choice((False, True))
                ),
            )
            for index in range(random.randint(0, 4))
        ]
        original = [unit.model_dump_json() for unit in units]
        result = pack(units, teams, target, minimum, maximum)
        assert result == pack(list(reversed(units)), teams, target, minimum, maximum)
        assert original == [unit.model_dump_json() for unit in units]
        assert Packing.model_validate_json(result.model_dump_json()) == result
        by_id = {unit.id: unit for unit in units}
        actual = [unit_id for filling in result.fills.values() for unit_id in filling]
        actual += [unit_id for team in result.new_teams for unit_id in team]
        actual += result.queue
        assert Counter(actual) == Counter(by_id.keys())
        assert result.queue == sorted(
            result.queue, key=lambda unit_id: (by_id[unit_id].queued_at, unit_id)
        )
        for team in result.new_teams:
            assert minimum <= sum(by_id[unit_id].size for unit_id in team) <= maximum
        for team in teams:
            filling = result.fills.get(team.id, [])
            assert set(filling).isdisjoint(team.banned_unit_ids)
            assert team.size + sum(by_id[unit_id].size for unit_id in filling) <= max(
                target, team.size
            )


@pytest.mark.parametrize("sizes", [(0, 3, 5), (4, 3, 5), (3, 4, 2)])
def test_invalid_sizes(sizes: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        pack([], [], sizes[1], sizes[0], sizes[2])


def test_contracts_are_frozen() -> None:
    unit = make_units([1])[0]
    attribute = "size"
    with pytest.raises(ValidationError):
        setattr(unit, attribute, 2)


def partitions(room: int, maximum: int, smallest: int = 1) -> Iterator[list[int]]:
    yield []
    for size in range(smallest, min(room, maximum) + 1):
        for tail in partitions(room - size, maximum, size):
            yield [size, *tail]


def test_exhaustive_parties_up_to_forty_people() -> None:
    for maximum in range(1, 6):
        for sizes in partitions(40, maximum):
            units = make_units(sizes)
            by_id = {unit.id: unit.size for unit in units}
            for minimum in (1, maximum):
                team = OpenTeam(
                    id=UUID(int=101),
                    size=maximum // 2,
                    banned_unit_ids=frozenset(unit.id for unit in units[::2]),
                )
                result = pack(units, [team], maximum, minimum, maximum)
                filling = result.fills.get(team.id, [])
                placed = [unit_id for new_team in result.new_teams for unit_id in new_team]
                assert Counter([*filling, *placed, *result.queue]) == Counter(by_id.keys())
                assert set(filling).isdisjoint(team.banned_unit_ids)
                assert team.size + sum(by_id[unit_id] for unit_id in filling) <= maximum
                assert all(
                    minimum <= sum(by_id[unit_id] for unit_id in new_team) <= maximum
                    for new_team in result.new_teams
                )
