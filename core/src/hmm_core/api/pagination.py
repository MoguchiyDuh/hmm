from typing import Annotated

from fastapi import Query


class CursorParams:
    def __init__(
        self,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> None:
        self.cursor = cursor
        self.limit = limit
