import itertools
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import procrastinate
from fastapi import FastAPI
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts.common import SEED_AREA_KEYS, OnboardingStatus
from hmm_core.app import create_app
from hmm_core.identity.service import SESSION_COOKIE, create_session
from hmm_core.models import Area, TelegramLink, User
from hmm_core.platform.clock import FixedClock
from hmm_core.platform.config import Config
from hmm_core.platform.db import create_sessionmaker
from hmm_core.platform.jobs import Runtime
from hmm_core.platform.storage import ObjectStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

PUBLIC_URL = "https://hmm.test"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
BOT_TOKEN = "bot-token"
PARSER_TOKEN = "parser-token"

_ids = itertools.count(10_000)


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.puts: list[str] = []
        self.gets: list[str] = []
        self.deleted: list[str] = []

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts.append(key)
        self.objects[key] = data
        self.content_types[key] = content_type

    async def get(self, key: str) -> bytes:
        self.gets.append(key)
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)
        self.content_types.pop(key, None)

    async def presign_get(self, key: str, expires: timedelta) -> str:
        return f"https://storage.test/{key}?expires={int(expires.total_seconds())}"


class Harness:
    def __init__(self, app: FastAPI, engine: AsyncEngine, clock: FixedClock) -> None:
        self.app = app
        self.engine = engine
        self.clock = clock
        self.sessionmaker: async_sessionmaker[AsyncSession] = create_sessionmaker(engine)
        self.algorithm = FakeAlgorithm()
        jobs: procrastinate.App = app.state.jobs
        self.jobs = jobs
        self.runtime = Runtime(
            config=app.state.config,
            sessionmaker=self.sessionmaker,
            jobs=jobs,
            algorithm=self.algorithm,
            clock=clock,
            log=logging.getLogger("hmm_core.tests"),
            http=self.app.state.http,
            storage=self.app.state.storage,
        )

    def inject_http(self, http: httpx.AsyncClient) -> None:
        self.app.state.http = http
        self.runtime = replace(self.runtime, http=http)

    def inject_storage(self, storage: ObjectStore) -> None:
        self.app.state.storage = storage
        self.runtime = replace(self.runtime, storage=storage)

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def advance(self, delta: timedelta) -> datetime:
        self.clock.set(self.clock.now() + delta)
        return self.clock.now()

    @asynccontextmanager
    async def db(self) -> AsyncGenerator[AsyncSession]:
        async with self.sessionmaker.begin() as session:
            yield session

    async def user(
        self,
        *,
        login: str | None = None,
        onboarded: bool = True,
        admin: bool = False,
        **fields: Any,
    ) -> User:
        number = next(_ids)
        name = login or f"user{number}"
        async with self.db() as session:
            user = User(
                github_id=number,
                github_login=name,
                display_name=fields.pop("display_name", name.title()),
                onboarding_status=OnboardingStatus.COMPLETED
                if onboarded
                else OnboardingStatus.NOT_STARTED,
                is_admin=admin,
                **fields,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
            return user

    async def seed_areas(self, keys: tuple[str, ...] = SEED_AREA_KEYS) -> dict[str, Area]:
        async with self.db() as session:
            areas = {
                key: Area(key=key, name=key.title(), description=key, sort=index * 10)
                for index, key in enumerate(keys, start=1)
            }
            session.add_all(areas.values())
            await session.flush()
            for area in areas.values():
                await session.refresh(area)
            return areas

    async def admin(self, **fields: Any) -> User:
        return await self.user(admin=True, **fields)

    async def link_telegram(self, user: User, telegram_user_id: int | None = None) -> int:
        identity = telegram_user_id or next(_ids)
        async with self.db() as session:
            session.add(
                TelegramLink(
                    user_id=user.id,
                    telegram_user_id=identity,
                    chat_id=identity,
                    username=f"tg{identity}",
                    linked_at=self.now,
                )
            )
        return identity

    def _client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url=PUBLIC_URL,
            **kwargs,
        )

    async def client(self, user: User) -> httpx.AsyncClient:
        async with self.db() as session:
            token = await create_session(
                session, user, self.now, timedelta(days=30), "pytest", None
            )
        client = self._client(headers={"Origin": PUBLIC_URL})
        client.cookies.set(SESSION_COOKIE, token)
        return client

    def anonymous(self) -> httpx.AsyncClient:
        return self._client(headers={"Origin": PUBLIC_URL})

    def bot(self, telegram_user_id: int | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {BOT_TOKEN}"}
        if telegram_user_id is not None:
            headers["X-Telegram-User-Id"] = str(telegram_user_id)
        return self._client(headers=headers)

    def parser(self) -> httpx.AsyncClient:
        return self._client(headers={"Authorization": f"Bearer {PARSER_TOKEN}"})

    async def run_jobs(self) -> None:
        await self.jobs.run_worker_async(
            wait=False,
            install_signal_handlers=False,
            additional_context={"runtime": self.runtime},
        )

    async def pending_jobs(self, task_name: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT task_name, args, status::text AS status, lock, queueing_lock "
            "FROM procrastinate.procrastinate_jobs"
        )
        params: dict[str, Any] = {}
        if task_name is not None:
            query += " WHERE task_name = :task_name"
            params["task_name"] = task_name
        async with self.engine.connect() as connection:
            rows = await connection.execute(text(query + " ORDER BY id"), params)
            return [dict(row._mapping) for row in rows]


def harness_config(database_url: str) -> Config:
    return Config(
        public_url=PUBLIC_URL,
        database_url=database_url,
        github_client_id="client-id",
        github_client_secret="client-secret",
        github_token="test-token",
        session_secret="session-secret",
        bot_service_token=BOT_TOKEN,
        parser_service_token=PARSER_TOKEN,
        telegram_bot_username="hmm_test_bot",
        algorithm="fake",
    )


@asynccontextmanager
async def open_harness(engine: AsyncEngine, database_url: str) -> AsyncGenerator[Harness]:
    app = create_app(harness_config(database_url))
    clock = FixedClock(NOW)
    app.state.clock = clock
    async with app.router.lifespan_context(app):
        jobs: procrastinate.App = app.state.jobs
        async with jobs.open_async():
            yield Harness(app, engine, clock)
