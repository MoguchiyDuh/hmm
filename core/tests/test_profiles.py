from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    ErrorCode,
    FlagStatus,
    FlagSubjectType,
    GithubSnapshotStatus,
    MemberReviewStatus,
    ReviewContext,
    ReviewSubjectType,
    Severity,
)
from hmm_core import models
from hmm_core.profiles import service as profiles_service
from test_api_skeleton import assert_problem


def review(
    h: Harness,
    reviewer: models.User,
    reviewee: models.User,
    context: ReviewContext,
    *,
    rating: int | None = 8,
    revealed: bool = True,
    removed: bool = False,
    created_at: datetime | None = None,
) -> models.Review:
    return models.Review(
        subject_type=ReviewSubjectType.PROJECT,
        subject_id=uuid4(),
        reviewer_id=reviewer.id,
        reviewee_id=reviewee.id,
        context=context,
        rating=rating,
        comment=f"{context} review",
        revealed_at=h.now if revealed else None,
        removed_at=h.now if removed else None,
        created_at=created_at or h.now,
    )


async def test_public_profile_fields_and_summary(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    actor = await h.user()
    target = await h.user(
        login="octocat",
        display_name="Octo",
        avatar_url="https://example.com/avatar.png",
        about_md="About me",
        contacts={"telegram": "@octo"},
        timezone="Europe/Moscow",
        locale="ru",
        dnd_indefinite=True,
    )
    async with h.db() as session:
        archived = await session.get(models.Area, areas["frontend"].id)
        assert archived is not None
        archived.archived_at = h.now
        session.add_all(
            [
                models.MemberMetric(
                    user_id=target.id,
                    xp=Decimal("12.5"),
                    activity=Decimal("3.25"),
                    algo_version="test",
                    input_hash="test",
                    computed_at=h.now,
                ),
                models.AreaScore(
                    user_id=target.id,
                    area_id=areas["backend"].id,
                    calculated=Decimal("4"),
                    calc_reason="Calculated",
                    override=Decimal("7"),
                    override_reason="Reviewed",
                ),
                models.AreaScore(
                    user_id=target.id,
                    area_id=areas["frontend"].id,
                    calculated=Decimal("9"),
                    calc_reason="Archived",
                ),
                models.MentorOffer(
                    user_id=target.id, area_id=areas["backend"].id, topics="SQL", active=True
                ),
            ]
        )
    async with await h.client(actor) as c:
        response = await c.get(f"/api/v1/users/{target.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(target.id)
    assert body["display_name"] == "Octo"
    assert body["avatar_url"] == target.avatar_url
    assert body["github_login"] == "octocat"
    assert body["github_url"] == "https://github.com/octocat"
    assert body["xp"] == 12.5
    assert body["activity"] == 3.25
    assert body["area_scores"] == [{"area_key": "backend", "effective": 7.0, "reason": "Reviewed"}]
    assert body["reputation"] == {
        context: {"avg": None, "count": 0} for context in ("member", "lead", "mentor", "host")
    }
    assert body["mentor_offers"] == [{"area": "backend", "topics": "SQL", "active": True}]
    assert body["dnd_until"] == "indefinite"
    assert body["allowed_actions"] == []
    assert body["about_md"] == "About me"
    assert body["contacts"] == {"telegram": "@octo"}
    assert body["timezone"] == "Europe/Moscow"
    assert body["locale"] == "ru"
    assert datetime.fromisoformat(body["created_at"]) == target.created_at
    assert datetime.fromisoformat(body["updated_at"]) == target.updated_at
    assert body["github"] is None
    assert body["github_status"] is None
    assert body["scores_reviewed_at"] is None
    assert body["reviews"] == []
    assert not {"cv", "notification_settings", "telegram", "flags", "score_history"} & body.keys()


async def test_profile_reads_canonical_github_snapshot_and_keeps_failed_sync_data(
    harness: Harness,
) -> None:
    h = harness
    member = await h.user(login="octocat")
    repos = [
        {
            "name": "hmm",
            "description": "A project",
            "topics": ["python", "rust"],
            "language": "Python",
            "stars": 9,
            "pushed_at": "2026-11-01T00:00:00Z",
        }
    ]
    async with await h.client(member) as c:
        response = await c.get(f"/api/v1/users/{member.id}")
        assert response.status_code == 200, response.text
        assert response.json()["github"] is None
        assert response.json()["github_status"] is None
        async with h.db() as session:
            session.add(
                models.GithubSnapshot(
                    user_id=member.id,
                    fetched_at=h.now,
                    status=GithubSnapshotStatus.OK,
                    profile={"login": "octocat", "account_created_at": "2020-01-02T03:04:05Z"},
                    repos=repos,
                    languages={"Python": 300, "Rust": 100},
                    stats={
                        "followers": 3,
                        "public_repos": 4,
                        "stars_total": 9,
                        "contributions_last_year": 40,
                    },
                )
            )
        response = await c.get(f"/api/v1/users/{member.id}")
        assert response.status_code == 200, response.text
        github = response.json()["github"]
        assert response.json()["github_status"] == "ok"
        assert github == {
            "login": "octocat",
            "account_created_at": "2020-01-02T03:04:05Z",
            "repos": repos,
            "languages": {"Python": 75.0, "Rust": 25.0},
            "followers": 3,
            "public_repos": 4,
            "stars_total": 9,
            "contributions_last_year": 40,
        }
        async with h.db() as session:
            snapshot = await session.get(models.GithubSnapshot, member.id)
            assert snapshot is not None
            snapshot.status = GithubSnapshotStatus.ERROR
            snapshot.error = "Sync failed"
        response = await c.get(f"/api/v1/users/{member.id}")
    assert response.status_code == 200, response.text
    assert response.json()["github_status"] == "error"
    assert response.json()["github"] == github


async def test_profile_shows_score_review_time_only_when_reviewed(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        session.add(
            models.MemberReview(
                user_id=member.id,
                status=MemberReviewStatus.PENDING,
                opened_at=h.now - timedelta(days=1),
                reviewed_at=h.now,
                discrepancies=[],
            )
        )
    async with await h.client(member) as c:
        response = await c.get(f"/api/v1/users/{member.id}")
        assert response.status_code == 200, response.text
        assert response.json()["scores_reviewed_at"] is None
        async with h.db() as session:
            member_review = await session.get(models.MemberReview, member.id)
            assert member_review is not None
            member_review.status = MemberReviewStatus.REVIEWED
        response = await c.get(f"/api/v1/users/{member.id}")
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["scores_reviewed_at"]) == h.now


async def test_profile_review_visibility_order_and_flags(harness: Harness) -> None:
    h = harness
    member = await h.user()
    reviewer = await h.user()
    member_review = review(
        h, reviewer, member, ReviewContext.MEMBER, created_at=h.now - timedelta(days=2)
    )
    lead_review = review(
        h, reviewer, member, ReviewContext.LEAD, created_at=h.now - timedelta(days=1)
    )
    mentor_review = review(h, reviewer, member, ReviewContext.MENTOR)
    hidden = review(h, reviewer, member, ReviewContext.MEMBER)
    async with h.db() as session:
        session.add_all(
            [
                member_review,
                lead_review,
                mentor_review,
                hidden,
                review(h, reviewer, member, ReviewContext.MEMBER, revealed=False),
                review(h, reviewer, member, ReviewContext.MEMBER, removed=True),
                review(h, reviewer, member, ReviewContext.HOST),
                review(h, reviewer, member, ReviewContext.SPEAKER),
            ]
        )
        await session.flush()
        for row, status in ((hidden, FlagStatus.TO_CHECK), (lead_review, FlagStatus.PARDONED)):
            session.add(
                models.Flag(
                    user_id=member.id,
                    subject_type=FlagSubjectType.REVIEW,
                    subject_id=row.id,
                    related_user_ids=[reviewer.id],
                    reason="Flagged review",
                    severity=Severity.LOW,
                    status=status,
                    algo_version="test",
                )
            )
        await session.refresh(member_review)
    async with await h.client(reviewer) as c:
        response = await c.get(f"/api/v1/users/{member.id}")
    assert response.status_code == 200, response.text
    reviews = response.json()["reviews"]
    assert [item["id"] for item in reviews] == [
        str(row.id) for row in (mentor_review, lead_review, member_review)
    ]
    assert [item["context"] for item in reviews] == ["mentor", "lead", "member"]
    assert [item["flag_status"] for item in reviews] == [None, "pardoned", None]
    assert reviews[-1] == {
        "id": str(member_review.id),
        "reviewer_id": str(reviewer.id),
        "reviewee_id": str(member.id),
        "context": "member",
        "rating": 8,
        "subject_type": "project",
        "subject_id": str(member_review.subject_id),
        "comment": "member review",
        "created_at": member_review.created_at.isoformat().replace("+00:00", "Z"),
        "revealed_at": h.now.isoformat().replace("+00:00", "Z"),
        "removed_at": None,
        "flag_status": None,
    }


async def test_profile_review_list_is_capped_and_breaks_timestamp_ties_by_id(
    harness: Harness,
) -> None:
    h = harness
    member = await h.user()
    reviewer = await h.user()
    rows = [review(h, reviewer, member, ReviewContext.MEMBER) for _ in range(22)]
    async with h.db() as session:
        session.add_all(rows)
        await session.flush()
    async with await h.client(reviewer) as c:
        response = await c.get(f"/api/v1/users/{member.id}")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["reviews"]] == [
        str(row.id) for row in sorted(rows, key=lambda row: row.id, reverse=True)[:20]
    ]


async def test_profile_host_reputation_folds_speaker_and_hides_small_samples(
    harness: Harness,
) -> None:
    h = harness
    member = await h.user()
    reviewer = await h.user()
    async with h.db() as session:
        session.add_all(
            [
                review(h, reviewer, member, ReviewContext.HOST, rating=6),
                review(h, reviewer, member, ReviewContext.HOST, rating=9),
            ]
        )
    async with await h.client(reviewer) as c:
        response = await c.get(f"/api/v1/users/{member.id}")
        assert response.status_code == 200, response.text
        assert response.json()["reputation"]["host"] == {"avg": None, "count": 2}
        assert response.json()["reviews"] == []
        async with h.db() as session:
            session.add(review(h, reviewer, member, ReviewContext.SPEAKER, rating=9))
        response = await c.get(f"/api/v1/users/{member.id}")
    assert response.status_code == 200, response.text
    assert response.json()["reputation"]["host"] == {"avg": 8.0, "count": 3}
    assert response.json()["reviews"] == []


async def test_profile_visibility_and_access(harness: Harness) -> None:
    h = harness
    actor = await h.user()
    pending = await h.user(onboarded=False)
    banned = await h.user(banned_at=h.now)
    async with await h.client(actor) as c:
        for user_id in (uuid4(), pending.id, banned.id):
            response = await c.get(f"/api/v1/users/{user_id}")
            assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with h.anonymous() as c:
        response = await c.get(f"/api/v1/users/{actor.id}")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(pending) as c:
        response = await c.get(f"/api/v1/users/{actor.id}")
    assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_owner_profile_update_persists_fields_and_enqueues_recompute(
    harness: Harness,
) -> None:
    h = harness
    member = await h.user()
    fields = {
        "display_name": "New",
        "about_md": "hi",
        "contacts": {"telegram": "@x"},
        "timezone": "Europe/Moscow",
        "locale": "ru",
    }
    async with await h.client(member) as c:
        response = await c.patch("/api/v1/me/profile", json=fields)
    assert response.status_code == 200, response.text
    for field, value in fields.items():
        assert response.json()[field] == value
    assert response.json()["allowed_actions"] == []
    async with h.db() as session:
        stored = await session.get(models.User, member.id)
        assert stored is not None
        for field, value in fields.items():
            assert getattr(stored, field) == value
        assert datetime.fromisoformat(response.json()["updated_at"]) == stored.updated_at
    jobs = await h.pending_jobs("recompute_member")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(member.id)}


async def test_partial_profile_updates_preserve_other_fields_and_debounce(harness: Harness) -> None:
    h = harness
    member = await h.user(
        about_md="Existing",
        contacts={"telegram": "@existing"},
        timezone="Europe/Moscow",
        locale="ru",
    )
    async with await h.client(member) as c:
        for name in ("OnlyName", "AnotherName"):
            response = await c.patch("/api/v1/me/profile", json={"display_name": name})
            assert response.status_code == 200, response.text
            assert response.json()["display_name"] == name
            assert response.json()["about_md"] == "Existing"
            assert response.json()["contacts"] == {"telegram": "@existing"}
            assert response.json()["timezone"] == "Europe/Moscow"
            assert response.json()["locale"] == "ru"
    async with h.db() as session:
        stored = await session.get(models.User, member.id)
        assert stored is not None
        assert stored.display_name == "AnotherName"
        assert stored.about_md == member.about_md
        assert stored.contacts == member.contacts
        assert stored.timezone == member.timezone
        assert stored.locale == member.locale
    assert len(await h.pending_jobs("recompute_member")) == 1


async def test_noop_profile_patch_does_not_write_or_enqueue(harness: Harness) -> None:
    h = harness
    member = await h.user(display_name="Same")
    async with await h.client(member) as c:
        for body in ({"display_name": "Same"}, {}):
            response = await c.patch("/api/v1/me/profile", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["display_name"] == "Same"
            assert datetime.fromisoformat(response.json()["updated_at"]) == member.updated_at
    async with h.db() as session:
        stored = await session.get(models.User, member.id)
        assert stored is not None
        assert stored.updated_at == member.updated_at
    assert await h.pending_jobs("recompute_member") == []


async def test_profile_patch_can_clear_timezone(harness: Harness) -> None:
    h = harness
    member = await h.user(timezone="Europe/Moscow")
    async with await h.client(member) as c:
        response = await c.patch("/api/v1/me/profile", json={"timezone": None})
    assert response.status_code == 200, response.text
    assert response.json()["timezone"] is None
    async with h.db() as session:
        stored = await session.get(models.User, member.id)
        assert stored is not None
        assert stored.timezone is None


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": None},
        {"about_md": None},
        {"contacts": None},
        {"locale": None},
        {"locale": "xx"},
        {"contacts": {f"contact{index}": "value" for index in range(11)}},
    ],
)
async def test_profile_patch_validation(harness: Harness, body: dict[str, Any]) -> None:
    h = harness
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.patch("/api/v1/me/profile", json=body)
    assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    assert await h.pending_jobs("recompute_member") == []


async def test_pre_onboarding_owner_can_edit_but_remains_invisible(harness: Harness) -> None:
    h = harness
    pending = await h.user(onboarded=False)
    member = await h.user()
    async with await h.client(pending) as c:
        response = await c.patch("/api/v1/me/profile", json={"display_name": "Early"})
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Early"
    async with h.db() as session:
        stored = await session.get(models.User, pending.id)
        assert stored is not None
        assert stored.display_name == "Early"
    jobs = await h.pending_jobs("recompute_member")
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(pending.id)}
    async with await h.client(member) as c:
        response = await c.get(f"/api/v1/users/{pending.id}")
    assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_failed_profile_response_rolls_back_edit_and_job(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    member = await h.user(display_name="Original")

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(profiles_service, "user_profile", boom)
    async with await h.client(member) as c:
        response = await c.patch("/api/v1/me/profile", json={"display_name": "Boom"})
    assert_problem(response, 500, ErrorCode.INTERNAL_ERROR)
    async with h.db() as session:
        stored = await session.get(models.User, member.id)
        assert stored is not None
        assert stored.display_name == "Original"
    assert await h.pending_jobs("recompute_member") == []
