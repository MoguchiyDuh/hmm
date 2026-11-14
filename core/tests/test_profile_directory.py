from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode, OnboardingStatus, ReviewContext, ReviewSubjectType
from hmm_core import models
from test_api_skeleton import assert_problem


def metric(h: Harness, user: models.User, xp: int, activity: int) -> models.MemberMetric:
    return models.MemberMetric(
        user_id=user.id,
        xp=Decimal(xp),
        activity=Decimal(activity),
        algo_version="test",
        input_hash="test",
        computed_at=h.now,
    )


def score(
    user: models.User,
    area: models.Area,
    calculated: str,
    *,
    override: str | None = None,
    override_reason: str | None = None,
) -> models.AreaScore:
    return models.AreaScore(
        user_id=user.id,
        area_id=area.id,
        calculated=Decimal(calculated),
        calc_reason="Calculated score",
        override=Decimal(override) if override is not None else None,
        override_reason=override_reason,
    )


async def test_directory_defaults_and_public_fields(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    first = await h.user(login="first", avatar_url="https://example.com/avatar.png")
    second = await h.user(login="second")
    third = await h.user(login="third")
    missing = await h.user(login="missing")
    await h.user(login="new", onboarded=False)
    await h.user(login="banned", banned_at=h.now)
    async with h.db() as session:
        session.add_all(
            [metric(h, first, 30, 2), metric(h, second, 20, 3), metric(h, third, 10, 1)]
        )
    async with await h.client(first) as c:
        response = await c.get("/api/v1/users")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [
        str(user.id) for user in (first, second, third, missing)
    ]
    assert body["next_cursor"] is None
    for item, xp, activity in zip(body["items"], (30, 20, 10, 0), (2, 3, 1, 0), strict=True):
        assert item["xp"] == xp
        assert item["activity"] == activity
        assert item["reputation"] == {
            context: {"avg": None, "count": 0} for context in ("member", "lead", "mentor", "host")
        }
        assert item["dnd_until"] is None
        assert item["area_scores"] == []
        assert item["mentor_offers"] == []
        assert item["github_url"] == f"https://github.com/{item['github_login']}"
    assert body["items"][0]["display_name"] == first.display_name
    assert body["items"][0]["avatar_url"] == first.avatar_url


async def test_directory_activity_and_case_insensitive_name_sort(harness: Harness) -> None:
    h = harness
    bob = await h.user(login="bob", display_name="bob")
    alice = await h.user(login="alice", display_name="Alice")
    carol = await h.user(login="carol", display_name="carol")
    async with h.db() as session:
        session.add_all([metric(h, bob, 30, 2), metric(h, alice, 20, 1), metric(h, carol, 10, 3)])
    async with await h.client(bob) as c:
        response = await c.get("/api/v1/users", params={"sort": "activity"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [
            str(user.id) for user in (carol, bob, alice)
        ]
        response = await c.get("/api/v1/users", params={"sort": "name", "limit": 2})
        assert response.status_code == 200, response.text
        assert [item["display_name"] for item in response.json()["items"]] == ["Alice", "bob"]
        response = await c.get(
            "/api/v1/users",
            params={"sort": "name", "cursor": response.json()["next_cursor"], "limit": 2},
        )
    assert response.status_code == 200, response.text
    assert [item["display_name"] for item in response.json()["items"]] == ["carol"]
    assert response.json()["next_cursor"] is None


async def test_directory_area_filter_and_archived_score_visibility(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    archived = await h.user()
    async with h.db() as session:
        area = await session.get(models.Area, areas["frontend"].id)
        assert area is not None
        area.archived_at = h.now
        session.add_all(
            [
                score(member, areas["backend"], "4", override="7", override_reason="Reviewed"),
                score(member, areas["design"], "6", override="8"),
                score(member, areas["frontend"], "9"),
                score(other, areas["design"], "5"),
                score(archived, areas["frontend"], "10"),
            ]
        )
    async with await h.client(member) as c:
        response = await c.get("/api/v1/users", params={"area": "backend"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(member.id)]
        expected = {
            "backend": {"area_key": "backend", "effective": 7.0, "reason": "Reviewed"},
            "design": {"area_key": "design", "effective": 8.0, "reason": "Calculated score"},
        }
        assert response.json()["items"][0]["area_scores"] == [
            expected[area.key]
            for area in sorted(
                (areas["backend"], areas["design"]), key=lambda area: (area.sort, area.key)
            )
        ]
        response = await c.get("/api/v1/users", params={"area": "frontend"})
    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()["items"]} == {str(member.id), str(archived.id)}
    assert all(
        score["area_key"] != "frontend"
        for item in response.json()["items"]
        for score in item["area_scores"]
    )


async def test_directory_minimum_uses_effective_non_archived_scores(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    await h.user()
    async with h.db() as session:
        area = await session.get(models.Area, areas["frontend"].id)
        assert area is not None
        area.archived_at = h.now
        session.add_all(
            [
                score(member, areas["backend"], "4", override="7"),
                score(other, areas["backend"], "5.5"),
                score(other, areas["design"], "6.9"),
                score(other, areas["frontend"], "10"),
            ]
        )
    async with await h.client(member) as c:
        for params in ({"area": "backend", "min_score": 6}, {"min_score": 7}):
            response = await c.get("/api/v1/users", params=params)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [str(member.id)]


async def test_directory_availability_expires_without_writes(harness: Harness) -> None:
    h = harness
    available = await h.user()
    indefinite = await h.user(dnd_indefinite=True)
    timed = await h.user(dnd_until=h.now + timedelta(days=1))
    expired = await h.user(dnd_until=h.now - timedelta(seconds=1))
    boundary = await h.user(dnd_until=h.now)
    async with await h.client(available) as c:
        for value, expected in (
            ("true", (available, expired, boundary)),
            ("false", (indefinite, timed)),
        ):
            response = await c.get("/api/v1/users", params={"available": value})
            assert response.status_code == 200, response.text
            assert {item["id"] for item in response.json()["items"]} == {
                str(user.id) for user in expected
            }
        h.advance(timedelta(days=2))
        response = await c.get("/api/v1/users", params={"available": "true"})
        assert response.status_code == 200, response.text
        items = {item["id"]: item for item in response.json()["items"]}
        assert set(items) == {str(user.id) for user in (available, timed, expired, boundary)}
        assert items[str(timed.id)]["dnd_until"] is not None
        assert items[str(timed.id)]["allowed_actions"] == ["request_help"]
        response = await c.get("/api/v1/users", params={"available": "false"})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(indefinite.id)]
    assert response.json()["items"][0]["dnd_until"] == "indefinite"


async def test_directory_mentor_filter_and_offer_summaries(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    mentor = await h.user()
    inactive = await h.user()
    other = await h.user()
    async with h.db() as session:
        session.add_all(
            [
                models.MentorOffer(
                    user_id=mentor.id, area_id=areas["backend"].id, topics="APIs", active=True
                ),
                models.MentorOffer(
                    user_id=mentor.id, area_id=areas["design"].id, topics="UX", active=False
                ),
                models.MentorOffer(
                    user_id=inactive.id, area_id=areas["backend"].id, topics="SQL", active=False
                ),
            ]
        )
    async with await h.client(mentor) as c:
        response = await c.get("/api/v1/users", params={"mentor": "true"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(mentor.id)]
        expected = {
            "backend": {"area": "backend", "topics": "APIs", "active": True},
            "design": {"area": "design", "topics": "UX", "active": False},
        }
        assert response.json()["items"][0]["mentor_offers"] == [
            expected[area.key]
            for area in sorted(
                (areas["backend"], areas["design"]), key=lambda area: (area.sort, area.key)
            )
        ]
        response = await c.get("/api/v1/users", params={"mentor": "false"})
    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()["items"]} == {str(inactive.id), str(other.id)}


async def test_directory_search_is_case_insensitive_and_literal(harness: Harness) -> None:
    h = harness
    member = await h.user(login="octocat", display_name="Alice 100% under_score path\\file")
    other = await h.user(login="other", display_name="1000 underXscore pathXfile")
    async with await h.client(member) as c:
        for term in (" aLiCe ", "OCTOCAT", "100%", "under_score", "path\\file"):
            response = await c.get("/api/v1/users", params={"q": term})
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [str(member.id)]
        for term in ("", "   "):
            response = await c.get("/api/v1/users", params={"q": term})
            assert response.status_code == 200, response.text
            assert {item["id"] for item in response.json()["items"]} == {
                str(member.id),
                str(other.id),
            }
        response = await c.get("/api/v1/users", params={"q": "nobody"})
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_directory_score_sort_and_unknown_area(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    first = await h.user()
    second = await h.user()
    third = await h.user()
    await h.user()
    async with h.db() as session:
        session.add_all(
            [
                score(first, areas["backend"], "4", override="8"),
                score(second, areas["backend"], "7"),
                score(third, areas["backend"], "9", override="0"),
            ]
        )
    async with await h.client(first) as c:
        response = await c.get("/api/v1/users", params={"sort": "score"})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
        response = await c.get("/api/v1/users", params={"area": "nope"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await c.get(
            "/api/v1/users", params={"sort": "score", "area": "backend", "limit": 2}
        )
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(first.id), str(second.id)]
        response = await c.get(
            "/api/v1/users",
            params={"sort": "score", "area": "backend", "cursor": response.json()["next_cursor"]},
        )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(third.id)]
    assert response.json()["items"][0]["area_scores"][0]["effective"] == 0


async def test_directory_reputation_sort_uses_visible_member_average_and_count(
    harness: Harness,
) -> None:
    h = harness
    high = await h.user()
    many = await h.user()
    tie_first = await h.user()
    tie_second = await h.user()
    empty = await h.user()
    reviewer = await h.user(onboarded=False)
    async with h.db() as session:
        for user, context, rating, revealed, removed in (
            (high, ReviewContext.MEMBER, 9, True, False),
            (many, ReviewContext.MEMBER, 7, True, False),
            (many, ReviewContext.MEMBER, 9, True, False),
            (tie_first, ReviewContext.MEMBER, 8, True, False),
            (tie_second, ReviewContext.MEMBER, 8, True, False),
            (empty, ReviewContext.MEMBER, 10, False, False),
            (empty, ReviewContext.MEMBER, 10, True, True),
            (empty, ReviewContext.MEMBER, None, True, False),
            (empty, ReviewContext.LEAD, 10, True, False),
            (high, ReviewContext.HOST, 7, True, False),
            (many, ReviewContext.HOST, 9, True, False),
            (many, ReviewContext.SPEAKER, 6, True, False),
            (many, ReviewContext.HOST, 9, True, False),
        ):
            session.add(
                models.Review(
                    subject_type=ReviewSubjectType.PROJECT,
                    subject_id=uuid4(),
                    reviewer_id=reviewer.id,
                    reviewee_id=user.id,
                    context=context,
                    rating=rating,
                    comment="",
                    revealed_at=h.now if revealed else None,
                    removed_at=h.now if removed else None,
                )
            )
    async with await h.client(high) as c:
        response = await c.get("/api/v1/users", params={"sort": "reputation"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        ties = sorted((tie_first, tie_second), key=lambda user: user.id, reverse=True)
        assert [item["id"] for item in items] == [
            str(user.id) for user in (high, many, *ties, empty)
        ]
        assert items[0]["reputation"]["host"] == {"avg": None, "count": 1}
        assert items[1]["reputation"]["host"] == {"avg": 8.0, "count": 3}
        assert items[-1]["reputation"]["member"] == {"avg": None, "count": 0}
        paginated = []
        cursor = None
        while True:
            params = {"sort": "reputation", "limit": "1"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await c.get("/api/v1/users", params=params)
            assert response.status_code == 200, response.text
            paginated.extend(response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
    assert paginated == items


async def test_directory_keyset_is_stable_when_a_higher_ranked_member_is_added(
    harness: Harness,
) -> None:
    h = harness
    first = await h.user()
    second = await h.user()
    third = await h.user()
    async with h.db() as session:
        session.add_all(
            [metric(h, first, 30, 1), metric(h, second, 20, 1), metric(h, third, 10, 1)]
        )
    async with await h.client(first) as c:
        response = await c.get("/api/v1/users", params={"sort": "xp", "limit": 2})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(first.id), str(second.id)]
        cursor = response.json()["next_cursor"]
        assert cursor is not None
        new = await h.user()
        async with h.db() as session:
            session.add(metric(h, new, 40, 1))
        response = await c.get("/api/v1/users", params={"sort": "xp", "limit": 2, "cursor": cursor})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(third.id)]
    assert response.json()["next_cursor"] is None


async def test_directory_allowed_actions_respect_self_and_dnd(harness: Harness) -> None:
    h = harness
    actor = await h.user()
    other = await h.user()
    indefinite = await h.user(dnd_indefinite=True)
    timed = await h.user(dnd_until=h.now + timedelta(days=1))
    async with h.db() as session:
        reopened = await session.get(models.User, other.id)
        assert reopened is not None
        reopened.onboarding_status = OnboardingStatus.REOPENED
    async with await h.client(actor) as c:
        response = await c.get("/api/v1/users")
    assert response.status_code == 200, response.text
    actions = {item["id"]: item["allowed_actions"] for item in response.json()["items"]}
    assert actions == {
        str(actor.id): [],
        str(other.id): ["request_help"],
        str(indefinite.id): [],
        str(timed.id): [],
    }


async def test_directory_requires_onboarding_and_valid_cursors(harness: Harness) -> None:
    h = harness
    async with h.anonymous() as c:
        response = await c.get("/api/v1/users")
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    pending = await h.user(onboarded=False)
    async with await h.client(pending) as c:
        response = await c.get("/api/v1/users")
    assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.get("/api/v1/users", params={"cursor": "abc"})
    assert_problem(response, 400, ErrorCode.BAD_REQUEST)


@pytest.mark.parametrize("sort", ["xp", "activity", "score", "reputation", "name"])
async def test_directory_malformed_cursor_is_rejected_for_every_sort(
    harness: Harness, sort: str
) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.get(
            "/api/v1/users", params={"sort": sort, "area": "backend", "cursor": "abc"}
        )
    assert_problem(response, 400, ErrorCode.BAD_REQUEST)
