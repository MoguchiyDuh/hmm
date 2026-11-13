import logging
import socket
from datetime import UTC, datetime, timedelta

import pytest
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts.algo import AlgoContext, Algorithm, AlgoSettings

IMPLEMENTATIONS: list[type[Algorithm]] = [FakeAlgorithm]


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    async def get(self, namespace: str, key: str) -> bytes | None:
        return self.values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: bytes, ttl: timedelta | None) -> None:
        self.values[namespace, key] = value

    async def delete(self, namespace: str, key: str) -> None:
        self.values.pop((namespace, key), None)


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda implementation: implementation.__name__)
def algorithm(request: pytest.FixtureRequest) -> Algorithm:
    implementation: type[Algorithm] = request.param
    return implementation()


@pytest.fixture
def ctx() -> AlgoContext:
    return AlgoContext(
        now=datetime(2026, 11, 12, 10, tzinfo=UTC),
        settings=AlgoSettings(),
        llm=None,
        cache=MemoryCache(),
        log=logging.getLogger("algo.conformance"),
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("Algorithm conformance must work without network or LLM access")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
