from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import Depends, Request


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.set(value)

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Clock requires a timezone-aware datetime")
        self._value = value.astimezone(UTC)


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


CurrentClock = Annotated[Clock, Depends(get_clock)]
