import asyncio
from collections.abc import Awaitable
from datetime import timedelta
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hmm.platform.clock import Clock
from hmm.platform.models import AlgoCache

type AlgoMethod = Literal[
    "score_onboarding",
    "compute_member",
    "recommend",
    "form_teams",
    "match_mentors",
    "scan_community",
]

ALGO_TIMEOUTS: dict[AlgoMethod, float] = {
    "score_onboarding": 60,
    "compute_member": 60,
    "recommend": 60,
    "form_teams": 120,
    "match_mentors": 60,
    "scan_community": 600,
}

CACHE_FOREVER = timedelta(days=36500)


class DatabaseAlgoCache:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock

    async def get(self, namespace: str, key: str) -> bytes | None:
        async with self._sessionmaker() as session:
            return await session.scalar(
                select(AlgoCache.value).where(
                    AlgoCache.namespace == namespace,
                    AlgoCache.key == key,
                    AlgoCache.expires_at > self._clock.now(),
                )
            )

    async def set(self, namespace: str, key: str, value: bytes, ttl: timedelta | None) -> None:
        expires_at = self._clock.now() + (CACHE_FOREVER if ttl is None else ttl)
        statement = insert(AlgoCache).values(
            namespace=namespace, key=key, value=value, expires_at=expires_at
        )
        async with self._sessionmaker.begin() as session:
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AlgoCache.namespace, AlgoCache.key],
                    set_={"value": value, "expires_at": expires_at},
                )
            )

    async def delete(self, namespace: str, key: str) -> None:
        async with self._sessionmaker.begin() as session:
            await session.execute(
                delete(AlgoCache).where(AlgoCache.namespace == namespace, AlgoCache.key == key)
            )


async def call_algo[T](method: AlgoMethod, awaitable: Awaitable[T]) -> T:
    async with asyncio.timeout(ALGO_TIMEOUTS[method]):
        return await awaitable
