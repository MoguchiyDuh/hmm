from typing import Any

from hmm_contracts.algo import GithubSummary

from hmm_core import models


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
