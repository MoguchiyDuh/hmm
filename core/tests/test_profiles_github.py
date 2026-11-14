import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode, GithubSnapshotStatus
from hmm_core.identity import service as identity
from hmm_core.identity.github import GithubUser
from hmm_core.models import GithubSnapshot, User
from hmm_core.platform.jobs import REGISTRY, JobName
from hmm_core.profiles import jobs as profile_jobs
from hmm_core.profiles import service as profiles
from hmm_core.profiles.github import github_summary
from hmm_core.profiles.github_api import GithubError
from sqlalchemy import text, update
from test_api_skeleton import assert_problem

REFRESH_URL = "/api/v1/me/github/refresh"


def repo_node(index: int) -> dict[str, Any]:
    return {
        "name": f"repo{index}",
        "description": None,
        "topics": ["a"],
        "stargazerCount": index,
        "pushedAt": "2024-01-02T03:04:05Z",
        "primaryLanguage": {"name": "Python"},
        "languages": {
            "edges": [
                {"size": 10, "node": {"name": "Python"}},
                {"size": 5, "node": {"name": "Rust"}},
            ]
        },
    }


def user_payload(login: str = "octo") -> dict[str, Any]:
    return {
        "login": login,
        "created_at": "2020-01-02T03:04:05Z",
        "followers": 12,
        "public_repos": 40,
    }


def repositories(nodes: list[dict[str, Any]], contributions: int) -> dict[str, Any]:
    return {
        "data": {
            "user": {
                "contributionsCollection": {
                    "contributionCalendar": {"totalContributions": contributions}
                },
                "repositories": {"nodes": nodes},
            }
        }
    }


class FakeGithub:
    def __init__(self, login: str = "octo") -> None:
        self.login = login
        self.user: Callable[[], httpx.Response] = lambda: httpx.Response(
            200, json=user_payload(login)
        )
        self.graphql: Callable[[], httpx.Response] = lambda: httpx.Response(
            200, json=repositories([repo_node(i) for i in range(120)], 123)
        )
        self.requests: list[httpx.Request] = []
        self.queries: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if str(request.url) == f"https://api.github.com/users/{self.login}":
            return self.user()
        if str(request.url) == "https://api.github.com/graphql":
            self.queries.append(json.loads(request.content))
            return self.graphql()
        return httpx.Response(404)


async def snapshot(harness: Harness, user: User) -> GithubSnapshot | None:
    async with harness.db() as session:
        return await session.get(GithubSnapshot, user.id)


async def github_user(harness: Harness, login: str = "octo") -> User:
    return await harness.user(login=login)


async def seed_snapshot(harness: Harness, user: User, **fields: Any) -> GithubSnapshot:
    row = GithubSnapshot(
        user_id=user.id,
        fetched_at=harness.now - timedelta(hours=2),
        status=GithubSnapshotStatus.OK,
        error=None,
        profile={"login": "octo", "account_created_at": "2020-01-02T03:04:05Z"},
        repos=[
            {
                "name": "old",
                "description": None,
                "topics": [],
                "language": "Go",
                "stars": 1,
                "pushed_at": "2023-01-01T00:00:00Z",
            }
        ],
        languages={"Go": 100},
        stats={"followers": 1, "stars_total": 1, "public_repos": 1, "contributions_last_year": 1},
        manual_refresh_at=None,
    )
    for name, value in fields.items():
        setattr(row, name, value)
    async with harness.db() as session:
        session.add(row)
    return row


async def test_sync_success(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        await profile_jobs.github_sync(harness.runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "ok"
    assert row.error is None
    assert row.fetched_at == harness.now
    assert row.profile == {"login": "octo", "account_created_at": "2020-01-02T03:04:05Z"}
    assert isinstance(row.repos, list)
    assert len(row.repos) == 100
    first = row.repos[0]
    assert isinstance(first, dict)
    assert first["name"] == "repo0"
    assert first["stars"] == 0
    assert row.languages == {"Python": 1000, "Rust": 500}
    assert row.stats == {
        "followers": 12,
        "stars_total": sum(range(100)),
        "public_repos": 40,
        "contributions_last_year": 123,
    }
    jobs = await harness.pending_jobs("recompute_member")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(user.id)}
    assert len(fake.queries) == 1
    assert fake.queries[0]["variables"] == {"login": "octo"}
    assert "contributionCalendar" in fake.queries[0]["query"]
    assert "languages" in fake.queries[0]["query"]


async def test_sync_feeds_github_summary(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        await profile_jobs.github_sync(harness.runtime, str(user.id))
    summary = github_summary(await snapshot(harness, user))
    assert summary is not None
    assert summary.login == "octo"
    assert len(summary.repos) == 100


async def test_sync_empty_repos(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    fake.graphql = lambda: httpx.Response(200, json=repositories([], 0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        await profile_jobs.github_sync(harness.runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "ok"
    assert row.repos == []
    assert row.languages == {}
    assert isinstance(row.stats, dict)
    assert row.stats["stars_total"] == 0


async def test_sync_replaces_snapshot(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        await profile_jobs.github_sync(harness.runtime, str(user.id))
        harness.advance(timedelta(hours=1))
        fake.graphql = lambda: httpx.Response(
            200, json=repositories([repo_node(1), repo_node(2)], 7)
        )
        await profile_jobs.github_sync(harness.runtime, str(user.id))
    async with harness.db() as session:
        count = await session.scalar(text("SELECT count(*) FROM github_snapshots"))
    assert count == 1
    row = await snapshot(harness, user)
    assert row is not None
    assert row.fetched_at == harness.now
    assert row.manual_refresh_at is None
    assert isinstance(row.repos, list)
    assert len(row.repos) == 2
    assert row.languages == {"Python": 20, "Rust": 10}
    assert row.stats == {
        "followers": 12,
        "stars_total": 3,
        "public_repos": 40,
        "contributions_last_year": 7,
    }
    assert len(await harness.pending_jobs("recompute_member")) == 1


async def test_rest_outage_keeps_previous(harness: Harness) -> None:
    user = await github_user(harness)
    seeded = await seed_snapshot(harness, user, manual_refresh_at=harness.now - timedelta(hours=3))
    fake = FakeGithub()
    fake.user = lambda: httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        with pytest.raises(GithubError):
            await profile_jobs.github_sync(harness.runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "error"
    assert row.error is not None
    assert "500" in row.error
    assert row.fetched_at == seeded.fetched_at
    assert row.manual_refresh_at == seeded.manual_refresh_at
    assert (row.profile, row.repos, row.languages, row.stats) == (
        seeded.profile,
        seeded.repos,
        seeded.languages,
        seeded.stats,
    )
    assert await harness.pending_jobs("recompute_member") == []


async def test_graphql_failure_without_previous(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    fake.graphql = lambda: httpx.Response(403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        with pytest.raises(GithubError):
            await profile_jobs.github_sync(harness.runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "error"
    assert row.error is not None
    assert "403" in row.error
    assert row.fetched_at == harness.now
    assert (row.profile, row.repos, row.languages, row.stats) == (None, None, None, None)


async def test_transport_timeout(harness: Harness) -> None:
    user = await github_user(harness)

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        harness.inject_http(http)
        with pytest.raises(httpx.ConnectTimeout):
            await profile_jobs.github_sync(harness.runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "error"
    assert row.error == "timed out"


async def test_missing_token(harness: Harness) -> None:
    user = await github_user(harness)
    fake = FakeGithub()
    config = harness.runtime.config.model_copy(update={"github_token": None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
        harness.inject_http(http)
        runtime = replace(harness.runtime, config=config)
        with pytest.raises(GithubError):
            await profile_jobs.github_sync(runtime, str(user.id))
    row = await snapshot(harness, user)
    assert row is not None
    assert row.status == "error"
    assert row.error == "GITHUB_TOKEN is not configured"
    assert fake.requests == []


async def test_manual_refresh(harness: Harness) -> None:
    throttled = await harness.user()
    seeded = await seed_snapshot(
        harness, throttled, manual_refresh_at=harness.now - timedelta(minutes=10)
    )
    async with await harness.client(throttled) as client:
        response = await client.post(REFRESH_URL)
    assert_problem(response, 429, ErrorCode.REFRESH_TOO_SOON)
    assert await harness.pending_jobs("github_sync") == []
    row = await snapshot(harness, throttled)
    assert row is not None
    assert row.manual_refresh_at == seeded.manual_refresh_at

    allowed = await harness.user()
    await seed_snapshot(harness, allowed, manual_refresh_at=harness.now - timedelta(minutes=61))
    async with await harness.client(allowed) as client:
        response = await client.post(REFRESH_URL)
    assert response.status_code == 202, response.text
    row = await snapshot(harness, allowed)
    assert row is not None
    assert row.manual_refresh_at == harness.now
    jobs = await harness.pending_jobs("github_sync")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(allowed.id)}
    assert jobs[0]["lock"] == f"user:{allowed.id}"

    fresh = await harness.user()
    async with await harness.client(fresh) as client:
        response = await client.post(REFRESH_URL)
    assert response.status_code == 202, response.text
    jobs = await harness.pending_jobs("github_sync")
    assert [job["args"] for job in jobs] == [
        {"user_id": str(allowed.id)},
        {"user_id": str(fresh.id)},
    ]
    assert await snapshot(harness, fresh) is None

    pending = await harness.user(onboarded=False)
    async with await harness.client(pending) as client:
        assert_problem(await client.post(REFRESH_URL), 403, ErrorCode.ONBOARDING_REQUIRED)
    async with harness.anonymous() as client:
        assert_problem(await client.post(REFRESH_URL), 401, ErrorCode.UNAUTHENTICATED)


async def test_sync_all(harness: Harness) -> None:
    first = await harness.user()
    second = await harness.user()
    banned = await harness.user(banned_at=harness.now)
    assert await profiles.sync_all_github(harness.runtime) == 2
    query = text(
        "SELECT args, scheduled_at, now() AS db_now FROM procrastinate.procrastinate_jobs "
        "WHERE task_name = 'github_sync' ORDER BY scheduled_at"
    )
    async with harness.engine.connect() as connection:
        rows = list((await connection.execute(query)).mappings())
    assert {row["args"]["user_id"] for row in rows} == {str(first.id), str(second.id)}
    assert str(banned.id) not in {row["args"]["user_id"] for row in rows}
    earliest, latest = rows[0]["scheduled_at"], rows[1]["scheduled_at"]
    assert abs((latest - earliest) - timedelta(hours=1)) < timedelta(seconds=5)
    assert abs(earliest - rows[0]["db_now"]) < timedelta(minutes=1)
    assert latest - earliest <= timedelta(hours=2)
    assert await profiles.sync_all_github(harness.runtime) == 2
    assert len(await harness.pending_jobs("github_sync")) == 2


async def clear_github_jobs(harness: Harness) -> None:
    async with harness.db() as session:
        await session.execute(
            text("DELETE FROM procrastinate.procrastinate_jobs WHERE task_name = 'github_sync'")
        )


async def test_login_triggers(harness: Harness) -> None:
    profile = GithubUser(id=9001, login="octo")
    async with harness.db() as session:
        user = await identity.sign_in(session, harness.jobs, profile, frozenset(), harness.now)
    jobs = await harness.pending_jobs("github_sync")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(user.id)}
    assert await snapshot(harness, user) is None
    await clear_github_jobs(harness)

    async with harness.db() as session:
        session.add(
            GithubSnapshot(
                user_id=user.id,
                fetched_at=harness.now - timedelta(hours=25),
                status=GithubSnapshotStatus.OK,
            )
        )
    async with harness.db() as session:
        await identity.sign_in(session, harness.jobs, profile, frozenset(), harness.now)
    assert len(await harness.pending_jobs("github_sync")) == 1
    await clear_github_jobs(harness)

    async with harness.db() as session:
        await session.execute(
            update(GithubSnapshot)
            .where(GithubSnapshot.user_id == user.id)
            .values(fetched_at=harness.now - timedelta(hours=1))
        )
    async with harness.db() as session:
        await identity.sign_in(session, harness.jobs, profile, frozenset(), harness.now)
    assert await harness.pending_jobs("github_sync") == []


async def test_github_jobs_registered(harness: Harness) -> None:
    assert JobName.GITHUB_SYNC in REGISTRY
    assert JobName.GITHUB_SYNC_ALL in REGISTRY
