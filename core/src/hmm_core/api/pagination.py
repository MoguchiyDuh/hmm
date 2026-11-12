import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from fastapi import Query
from hmm_contracts.common import ErrorCode, Page
from pydantic import TypeAdapter
from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from hmm_core.api.errors import ProblemError

type Column = InstrumentedAttribute[Any]


class CursorParams:
    def __init__(
        self,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> None:
        self.cursor = cursor
        self.limit = limit


def encode_cursor(values: Sequence[Any]) -> str:
    payload = json.dumps([str(value) for value in values]).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def decode_cursor(cursor: str, columns: Sequence[Column]) -> list[Any]:
    try:
        items = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(items, list) or len(items) != len(columns):
            raise ValueError("Cursor does not match the sort key")
        return [
            TypeAdapter(column.type.python_type).validate_python(item)
            for column, item in zip(columns, items, strict=True)
        ]
    except (ValueError, TypeError, binascii.Error) as error:
        raise ProblemError(ErrorCode.BAD_REQUEST, "Malformed pagination cursor") from error


def apply_keyset[T](
    statement: Select[T],
    params: CursorParams,
    *,
    sort: Sequence[Column],
    id_column: Column,
    descending: bool = True,
) -> Select[T]:
    columns = [*sort, id_column]
    if params.cursor is not None:
        key = tuple_(*columns)
        values = tuple_(*decode_cursor(params.cursor, columns))
        statement = statement.where(key < values if descending else key > values)
    return (
        statement.order_by(None)
        .order_by(*(column.desc() if descending else column.asc() for column in columns))
        .limit(params.limit + 1)
    )


async def keyset_page[T, R](
    session: AsyncSession,
    statement: Select[T],
    params: CursorParams,
    *,
    sort: Sequence[Column],
    id_column: Column,
    serialize: Callable[[T], R],
    descending: bool = True,
) -> Page[R]:
    rows = list(
        await session.scalars(
            apply_keyset(statement, params, sort=sort, id_column=id_column, descending=descending)
        )
    )
    items = rows[: params.limit]
    next_cursor = None
    if len(rows) > params.limit:
        next_cursor = encode_cursor(
            [getattr(items[-1], column.key) for column in [*sort, id_column]]
        )
    return Page(items=[serialize(row) for row in items], next_cursor=next_cursor)
