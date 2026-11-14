from datetime import UTC, datetime
from typing import Any

import pytest
from hmm_contracts.common import GithubSnapshotStatus
from hmm_core.models import GithubSnapshot
from hmm_core.profiles.github import github_summary, language_shares

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def snapshot(**fields: Any) -> GithubSnapshot:
    values: dict[str, Any] = {
        "fetched_at": NOW,
        "status": GithubSnapshotStatus.OK,
        "profile": {"login": "octo", "account_created_at": "2020-01-02T03:04:05Z"},
        "repos": [
            {
                "name": "hmm",
                "description": None,
                "topics": ["python"],
                "language": "Python",
                "stars": 5,
                "pushed_at": "2026-11-01T00:00:00Z",
            },
            {"name": "bare"},
        ],
        "languages": {"Python": 300, "Rust": 100, "C": 0},
        "stats": {
            "followers": 12,
            "stars_total": 5,
            "public_repos": 40,
            "contributions_last_year": 321,
        },
    }
    values.update(fields)
    return GithubSnapshot(**values)


def test_summary_reads_stored_layout() -> None:
    summary = github_summary(snapshot())
    assert summary is not None
    assert summary.login == "octo"
    assert summary.account_created_at == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert summary.languages == {"Python": 75.0, "Rust": 25.0}
    assert [repo.name for repo in summary.repos] == ["hmm", "bare"]
    assert summary.repos[1].topics == []
    assert summary.repos[1].stars == 0
    assert (
        summary.followers,
        summary.stars_total,
        summary.public_repos,
        summary.contributions_last_year,
    ) == (12, 5, 40, 321)


def test_failed_sync_keeps_previous_snapshot() -> None:
    summary = github_summary(snapshot(status=GithubSnapshotStatus.ERROR, error="boom"))
    assert summary is not None
    assert summary.login == "octo"


def test_missing_stats_default_to_zero() -> None:
    summary = github_summary(snapshot(stats={}, repos=[], languages={}))
    assert summary is not None
    assert summary.languages == {}
    assert summary.stars_total == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"profile": None},
        {"stats": None},
        {"repos": None},
        {"languages": None},
        {"profile": {"login": "octo"}},
        {"profile": {"login": "octo", "account_created_at": "yesterday"}},
        {"stats": {"followers": -1}},
        {"languages": {"Python": "many"}},
        {"repos": [{"description": "no name"}]},
    ],
)
def test_incomplete_snapshot_is_none(fields: dict[str, Any]) -> None:
    assert github_summary(snapshot(**fields)) is None


def test_no_row_is_none() -> None:
    assert github_summary(None) is None


def test_language_shares_round() -> None:
    assert language_shares({"a": 1, "b": 2}) == {"a": 33.33, "b": 66.67}
