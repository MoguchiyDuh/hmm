import importlib
import logging
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

from pydantic import ValidationError

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat
from hmm.parser.config import SourceConfig, SourceConfigError
from hmm.parser.fetch import Fetched, Fetcher, Params
from hmm.parser.normalize import EUROPE

EMPTY = (None, "", [], {})


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    url: str
    payload: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceContext:
    source: SourceConfig
    fetcher: Fetcher
    now: datetime
    areas: frozenset[str]
    env: Mapping[str, str]
    log: logging.Logger

    @property
    def cutoff(self) -> datetime:
        return self.now - timedelta(days=self.source.lookback_days)

    async def get(
        self,
        url: str,
        *,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        accept: frozenset[int] = frozenset({200}),
        rate_key: str | None = None,
    ) -> Fetched:
        return await self.fetcher.get(
            url,
            spacing=self.source.spacing,
            request_timeout=self.source.timeout,
            max_bytes=self.source.max_bytes,
            params=params,
            headers=headers,
            accept=accept,
            rate_key=rate_key,
        )

    async def post(
        self,
        url: str,
        body: Any,
        *,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        accept: frozenset[int] = frozenset({200}),
        rate_key: str | None = None,
    ) -> Fetched:
        return await self.fetcher.request(
            "POST",
            url,
            spacing=self.source.spacing,
            request_timeout=self.source.timeout,
            max_bytes=self.source.max_bytes,
            params=params,
            headers=headers,
            body=body,
            accept=accept,
            rate_key=rate_key,
        )

    def in_scope(self, country: str | None, fmt: EventFormat | None) -> bool:
        if country is not None:
            allowed = self.source.countries or EUROPE
            if country in allowed:
                return True
        online = fmt in {EventFormat.ONLINE, EventFormat.HYBRID}
        return online and self.source.include_online

    def confidence(self, default: float) -> float:
        return self.source.confidence if self.source.confidence is not None else default


class NoOptions(Contract):
    pass


class Adapter[O: Contract](ABC):
    name: ClassVar[str]
    version: ClassVar[int] = 1
    options_model: ClassVar[type[Contract]] = NoOptions

    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = cast(O, self.options_model.model_validate(dict(options)))

    @property
    def extractor_version(self) -> str:
        return f"{self.name}/{self.version}"

    def warnings(self, ctx: SourceContext) -> list[str]:
        return []

    @abstractmethod
    def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]: ...

    @abstractmethod
    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]: ...

    def build(
        self,
        ctx: SourceContext,
        local_id: str,
        urls: list[str],
        fields: dict[str, Any],
    ) -> EventImport:
        values = {key: value for key, value in fields.items() if value not in EMPTY}
        for key, value in ctx.source.defaults.items():
            if values.get(key) in EMPTY:
                values[key] = value
        values.setdefault("extractor_version", self.extractor_version)
        return EventImport.model_validate(
            {
                "source": {
                    "source_id": f"{ctx.source.id}:{local_id}",
                    "urls": list(dict.fromkeys(urls)),
                    "fetched_at": ctx.now,
                },
                **values,
            }
        )


ADAPTERS: dict[str, type[Adapter[Any]]] = {}


def adapter[A: type[Adapter[Any]]](cls: A) -> A:
    if cls.name in ADAPTERS and ADAPTERS[cls.name] is not cls:
        raise RuntimeError(f"Adapter {cls.name} is already registered")
    ADAPTERS[cls.name] = cls
    return cls


def load_adapters() -> dict[str, type[Adapter[Any]]]:
    from hmm.parser import sources

    for module in pkgutil.iter_modules(sources.__path__):
        importlib.import_module(f"{sources.__name__}.{module.name}")
    return ADAPTERS


def create_adapter(source: SourceConfig) -> Adapter[Any]:
    cls = load_adapters().get(source.adapter)
    if cls is None:
        raise SourceConfigError(f"Unknown adapter {source.adapter!r} for source {source.id!r}")
    try:
        return cls(source.options)
    except ValidationError as error:
        raise SourceConfigError(f"Invalid options for source {source.id!r}: {error}") from error
