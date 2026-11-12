from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import DateTime, Text, cast, type_coerce
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import TypeDecorator

type DndTimestamp = datetime | Literal["indefinite"]


class DndUntil(TypeDecorator[DndTimestamp]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: DndTimestamp | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value == "indefinite":
            return "infinity"
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("DND requires an aware datetime or 'indefinite'")
        return value.astimezone(UTC)

    def column_expression(self, column: ColumnElement[Any]) -> ColumnElement[DndTimestamp]:
        return type_coerce(cast(column, Text), self)

    def process_result_value(self, value: Any, dialect: Dialect) -> DndTimestamp | None:
        if value is None:
            return None
        if value == "infinity":
            return "indefinite"
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return stamp.astimezone(UTC)
