from pydantic import AwareDatetime, Field

from hmm.common import Contract, ProjectId, UnitId

__all__ = ["OpenTeam", "Packing", "Unit", "pack"]


class Unit(Contract):
    id: UnitId
    size: int = Field(ge=1)
    queued_at: AwareDatetime


class OpenTeam(Contract):
    id: ProjectId
    size: int = Field(ge=0)
    banned_unit_ids: frozenset[UnitId] = frozenset()


class Packing(Contract):
    fills: dict[ProjectId, list[UnitId]]
    new_teams: list[list[UnitId]]
    queue: list[UnitId]


def pack(units: list[Unit], teams: list[OpenTeam], t: int, min_size: int, max_size: int) -> Packing:
    if not 1 <= min_size <= t <= max_size:
        raise ValueError("sizes must satisfy 1 <= min_size <= t <= max_size")
    if any(unit.size > max_size for unit in units):
        raise ValueError("unit size must not exceed max_size")
    if any(team.size > max_size for team in teams):
        raise ValueError("team size must not exceed max_size")
    if len({unit.id for unit in units}) != len(units):
        raise ValueError("unit ids must be unique")
    if len({team.id for team in teams}) != len(teams):
        raise ValueError("team ids must be unique")
    queue = sorted(units, key=lambda unit: (unit.queued_at, unit.id))
    placed: set[UnitId] = set()
    fills: dict[ProjectId, list[UnitId]] = {}
    for team in teams:
        room = t - team.size
        filling: list[UnitId] = []
        for unit in queue:
            if unit.id not in placed and unit.id not in team.banned_unit_ids and unit.size <= room:
                filling.append(unit.id)
                placed.add(unit.id)
                room -= unit.size
        if filling:
            fills[team.id] = filling
    remaining = [unit for unit in queue if unit.id not in placed]
    people = sum(unit.size for unit in remaining)
    lower = (people + max_size - 1) // max_size
    upper = people // min_size
    count = min(max(round(people / t), lower), upper) if lower <= upper else upper
    ordered = sorted(remaining, key=lambda unit: (-unit.size, unit.queued_at, unit.id))
    new_teams: list[list[UnitId]] = []
    sizes: list[int] = []
    for direction in (1, -1):
        candidate: list[list[UnitId]] = [[] for _ in range(count)]
        candidate_sizes = [0] * count
        for unit in ordered:
            index = min(
                (index for index in range(count) if candidate_sizes[index] + unit.size <= max_size),
                key=lambda index: (direction * candidate_sizes[index], index),
                default=None,
            )
            if index is not None:
                candidate[index].append(unit.id)
                candidate_sizes[index] += unit.size
        if sum(candidate_sizes) > sum(sizes):
            new_teams, sizes = candidate, candidate_sizes
        if sum(candidate_sizes) == people:
            break
    new_teams = [team for team, size in zip(new_teams, sizes, strict=True) if size >= min_size]
    placed.update(unit_id for team in new_teams for unit_id in team)
    return Packing(
        fills=fills,
        new_teams=new_teams,
        queue=[unit.id for unit in queue if unit.id not in placed],
    )
