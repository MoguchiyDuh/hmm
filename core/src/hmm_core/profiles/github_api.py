from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import JsonValue

API = "https://api.github.com"
REST_USER = f"{API}/users/{{login}}"
GRAPHQL = f"{API}/graphql"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "hmm-core",
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
