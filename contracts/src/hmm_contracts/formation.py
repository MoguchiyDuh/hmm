from pydantic import Field

from hmm_contracts.common import Contract

__all__ = ["Bounds", "placement_bounds"]


class Bounds(Contract):
    teams: int = Field(ge=0)
    new_teams: int = Field(ge=0)
    placed: int = Field(ge=0)
    reserve: int = Field(ge=0)
    undersized_cores: int = Field(ge=0)


def placement_bounds(free: int, cores: list[int], v: int, t: int, max_size: int) -> Bounds:
    if free < 0:
        raise ValueError("free must be nonnegative")
    if v < 1:
        raise ValueError("v must be at least 1")
    if v > max_size:
        raise ValueError("v must not exceed max_size")
    if not v <= t <= max_size:
        raise ValueError("t must be between v and max_size")
    for index, size in enumerate(cores):
        if not 1 <= size <= max_size:
            raise ValueError(f"core size at index {index} must be between 1 and max_size")

    deficits = sorted(v - size for size in cores if size < v)
    need = sum(deficits)
    if free <= need:
        remaining = free
        undersized = len(deficits)
        for deficit in deficits:
            if remaining < deficit:
                break
            remaining -= deficit
            undersized -= 1
        return Bounds(
            teams=len(cores),
            new_teams=0,
            placed=free,
            reserve=0,
            undersized_cores=undersized,
        )

    rest = free - need
    extra = sum(max_size - max(size, v) for size in cores)
    new_teams = 0
    placed = need + min(rest, extra)
    for count in range(1, rest // v + 1):
        candidate = need + min(rest, extra + count * max_size)
        if candidate > placed:
            new_teams = count
            placed = candidate
    return Bounds(
        teams=len(cores) + new_teams,
        new_teams=new_teams,
        placed=placed,
        reserve=free - placed,
        undersized_cores=0,
    )
