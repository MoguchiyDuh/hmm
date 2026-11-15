from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import procrastinate
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import GithubSummary
from hmm.common import ErrorCode, GithubSnapshotStatus
from hmm.identity.service import lock_user
from hmm.platform.db import locked
from hmm.platform.errors import ProblemError
from hmm.platform.jobs import JobName, Runtime, defer_job
from hmm.platform.settings import load_settings

API = "https://api.github.com"
REST_USER = f"{API}/users/{{login}}"
GRAPHQL = f"{API}/graphql"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "hmm",
}
REPOSITORIES_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection { contributionCalendar { totalContributions } }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name description topics stargazerCount pushedAt
        primaryLanguage { name }
        languages(first: 20) { edges { size node { name } } }
      }
    }
  }
}
"""


class GithubError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GithubData:
    profile: dict[str, JsonValue]
    repos: list[dict[str, JsonValue]]
    languages: dict[str, int]
    stats: dict[str, int]


def _json(response: httpx.Response, message: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise GithubError(message) from error
    if not isinstance(payload, dict):
        raise GithubError(message)
    return payload


async def fetch_data(http: httpx.AsyncClient, *, token: str | None, login: str) -> GithubData:
    if not token:
        raise GithubError("GITHUB_TOKEN is not configured")
    headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    response = await http.get(REST_USER.format(login=login), headers=headers)
    if not response.is_success:
        raise GithubError(f"GitHub user request failed: {response.status_code}")
    user_payload = _json(response, "GitHub user response is invalid")
    rest_login = user_payload.get("login")
    created_at = user_payload.get("created_at")
    followers = user_payload.get("followers")
    public_repos = user_payload.get("public_repos")
    if not (
        isinstance(rest_login, str)
        and isinstance(created_at, str)
        and isinstance(followers, int)
        and isinstance(public_repos, int)
    ):
        raise GithubError("GitHub user response is incomplete")
    response = await http.post(
        GRAPHQL,
        headers=headers,
        json={"query": REPOSITORIES_QUERY, "variables": {"login": login}},
    )
    if not response.is_success:
        raise GithubError(f"GitHub GraphQL request failed: {response.status_code}")
    payload = _json(response, "GitHub GraphQL response is invalid")
    try:
        if payload.get("errors"):
            raise GithubError("GitHub GraphQL response is invalid")
        user = payload["data"]["user"]
        nodes = list(user["repositories"]["nodes"])[:100]
        contributions = int(
            user["contributionsCollection"]["contributionCalendar"]["totalContributions"]
        )
        stars = [int(node.get("stargazerCount") or 0) for node in nodes]
        repos: list[dict[str, JsonValue]] = [
            {
                "name": node["name"],
                "description": node.get("description"),
                "topics": list(node.get("topics") or []),
                "language": (node.get("primaryLanguage") or {}).get("name"),
                "stars": count,
                "pushed_at": node.get("pushedAt"),
            }
            for node, count in zip(nodes, stars, strict=True)
        ]
        totals: defaultdict[str, int] = defaultdict(int)
        for node in nodes:
            for edge in (node.get("languages") or {}).get("edges") or []:
                totals[edge["node"]["name"]] += int(edge["size"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise GithubError("GitHub GraphQL response is invalid") from error
    languages = {name: totals[name] for name in sorted(totals) if totals[name]}
    return GithubData(
        profile={"login": rest_login, "account_created_at": created_at},
        repos=repos,
        languages=languages,
        stats={
            "followers": followers,
            "stars_total": sum(stars),
            "public_repos": public_repos,
            "contributions_last_year": contributions,
        },
    )


def language_shares(languages: dict[str, Any]) -> dict[str, float]:
    sizes = {name: int(size) for name, size in languages.items() if int(size) > 0}
    total = sum(sizes.values())
    if total == 0:
        return {}
    return {name: round(size * 100 / total, 2) for name, size in sizes.items()}


def github_summary(row: models.GithubSnapshot | None) -> GithubSummary | None:
    if row is None:
        return None
    profile, stats, repos, languages = row.profile, row.stats, row.repos, row.languages
    if not (
        isinstance(profile, dict)
        and isinstance(stats, dict)
        and isinstance(repos, list)
        and isinstance(languages, dict)
    ):
        return None
    try:
        return GithubSummary.model_validate(
            {
                "login": profile["login"],
                "account_created_at": profile["account_created_at"],
                "languages": language_shares(languages),
                "repos": [
                    {
                        "name": repo["name"],
                        "description": repo.get("description"),
                        "topics": repo.get("topics", []),
                        "language": repo.get("language"),
                        "stars": repo.get("stars", 0),
                        "pushed_at": repo.get("pushed_at"),
                    }
                    for repo in repos
                    if isinstance(repo, dict)
                ],
                "followers": stats.get("followers", 0),
                "contributions_last_year": stats.get("contributions_last_year", 0),
                "public_repos": stats.get("public_repos", 0),
                "stars_total": stats.get("stars_total", 0),
            }
        )
    except (KeyError, TypeError, ValueError):
        return None


async def refresh_github(
    session: AsyncSession, jobs: procrastinate.App, *, user_id: UUID, now: datetime
) -> None:
    await lock_user(session, user_id)
    row = await locked(session, models.GithubSnapshot, user_id)
    if row is not None:
        interval = timedelta(
            minutes=(await load_settings(session)).github_manual_refresh_min_interval_minutes
        )
        if row.manual_refresh_at is not None and now - row.manual_refresh_at < interval:
            raise ProblemError(ErrorCode.REFRESH_TOO_SOON)
        row.manual_refresh_at = now
    await defer_job(session, jobs, JobName.GITHUB_SYNC, user_id=str(user_id))


async def record_github_failure(
    session: AsyncSession, user_id: UUID, error: str, now: datetime
) -> None:
    row = await locked(session, models.GithubSnapshot, user_id)
    if row is not None:
        row.status = GithubSnapshotStatus.ERROR
        row.error = error[:1000]
        return
    session.add(
        models.GithubSnapshot(
            user_id=user_id,
            fetched_at=now,
            status=GithubSnapshotStatus.ERROR,
            error=error[:1000],
            profile=None,
            repos=None,
            languages=None,
            stats=None,
            manual_refresh_at=None,
        )
    )


async def store_github_snapshot(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    user_id: UUID,
    data: GithubData,
    now: datetime,
) -> models.GithubSnapshot:
    row = await locked(session, models.GithubSnapshot, user_id)
    if row is None:
        row = models.GithubSnapshot(user_id=user_id, manual_refresh_at=None)
        session.add(row)
    row.fetched_at = now
    row.status = GithubSnapshotStatus.OK
    row.error = None
    row.profile = data.profile
    row.repos = list(data.repos)
    row.languages = dict(data.languages)
    row.stats = dict(data.stats)
    await session.flush()
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return row


async def sync_github(runtime: Runtime, user_id: UUID) -> None:
    async with runtime.sessionmaker() as session:
        login = await session.scalar(
            select(models.User.github_login).where(models.User.id == user_id)
        )
    if login is None:
        return
    try:
        data = await fetch_data(runtime.http, token=runtime.config.github_token, login=login)
    except Exception as error:
        async with runtime.sessionmaker.begin() as session:
            await record_github_failure(session, user_id, str(error), runtime.clock.now())
        raise
    async with runtime.sessionmaker.begin() as session:
        await store_github_snapshot(
            session, runtime.jobs, user_id=user_id, data=data, now=runtime.clock.now()
        )


async def sync_all_github(runtime: Runtime) -> int:
    async with runtime.sessionmaker.begin() as session:
        ids = list(
            await session.scalars(
                select(models.User.id)
                .where(models.User.banned_at.is_(None))
                .order_by(models.User.id)
            )
        )
        for index, uid in enumerate(ids):
            await defer_job(
                session,
                runtime.jobs,
                JobName.GITHUB_SYNC,
                user_id=str(uid),
                schedule_in=2 * 3600 * index / len(ids),
            )
    return len(ids)
