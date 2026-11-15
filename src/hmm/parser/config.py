import re
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, JsonValue, ValidationError, field_validator

from hmm.common import Contract

DEFAULT_SOURCES = Path(__file__).with_name("sources.toml")

_DURATION = re.compile(r"^(\d+)\s*(s|m|h|d)$")
_RATE = re.compile(r"^(\d+)\s*/\s*(\d*)\s*(s|sec|min|m|h)$")
_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}


class SourceConfigError(ValueError):
    pass


def parse_duration(value: str) -> timedelta:
    match = _DURATION.match(value.strip())
    if match is None:
        raise ValueError(f"Invalid duration {value!r}, expected e.g. '30m', '12h', '1d'")
    return timedelta(seconds=int(match[1]) * _UNITS[match[2]])


def parse_rate(value: str) -> float:
    match = _RATE.match(value.strip())
    if match is None:
        raise ValueError(f"Invalid rate {value!r}, expected e.g. '20/min', '1/10s'")
    count, span, unit = int(match[1]), int(match[2] or 1), match[3]
    if count < 1 or span < 1:
        raise ValueError(f"Invalid rate {value!r}")
    return span * _UNITS[unit] / count


class SourceConfig(Contract):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    adapter: Annotated[str, Field(min_length=1)]
    enabled: bool = True
    interval: timedelta = timedelta(hours=12)
    spacing: Annotated[float, Field(ge=0)] = 1.0
    timeout: Annotated[float, Field(gt=0)] = 30.0
    max_bytes: Annotated[int, Field(gt=0)] = 8 * 1024 * 1024
    budget: timedelta = timedelta(minutes=10)
    priority: int = 50
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    countries: frozenset[str] = frozenset()
    include_online: bool = True
    lookback_days: Annotated[int, Field(ge=0)] = 30
    llm: bool = False
    secret_env: tuple[str, ...] = ()
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("countries")
    @classmethod
    def upper_countries(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(code.upper() for code in value)

    def missing_secrets(self, env: Mapping[str, str]) -> list[str]:
        return [name for name in self.secret_env if not env.get(name)]


_SHARED = frozenset(SourceConfig.model_fields) - {"options", "interval", "spacing", "budget"}


def source_from_table(table: Mapping[str, Any]) -> SourceConfig:
    shared: dict[str, Any] = {}
    options: dict[str, Any] = {}
    for key, value in table.items():
        if key in _SHARED:
            shared[key] = value
        elif key == "interval":
            shared["interval"] = parse_duration(value)
        elif key == "budget":
            shared["budget"] = parse_duration(value)
        elif key == "rate":
            shared["spacing"] = parse_rate(value)
        else:
            options[key] = value
    return SourceConfig(**shared, options=options)


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    target = path or DEFAULT_SOURCES
    try:
        document = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SourceConfigError(f"Cannot read parser sources from {target}: {error}") from error
    tables = document.get("source", [])
    if not isinstance(tables, list):
        raise SourceConfigError(f"{target}: [[source]] must be an array of tables")
    sources: list[SourceConfig] = []
    for index, table in enumerate(tables):
        try:
            sources.append(source_from_table(table))
        except (ValidationError, ValueError) as error:
            raise SourceConfigError(f"{target}: source #{index + 1}: {error}") from error
    ids = [source.id for source in sources]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise SourceConfigError(f"{target}: duplicate source ids {duplicates}")
    return sources
