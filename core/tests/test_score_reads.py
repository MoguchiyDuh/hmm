from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import ScoreWithReason
from hmm_contracts.common import AreaScoreEventKind, ErrorCode, OverrideEvidence
from hmm_core.models import Area, AreaScoreEvent
from hmm_core.scores import schemas
from hmm_core.scores.service import set_score_override, write_calculated_scores
from test_api_skeleton import assert_problem


async def test_owner_scores_order_and_archived_areas(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    async with h.db() as session:
        await write_calculated_scores(
            session,
            member.id,
            {
                "backend": ScoreWithReason(value=6.5, reason="quiz"),
                "data": ScoreWithReason(value=3.0, reason="cv"),
                "qa": ScoreWithReason(value=4.0, reason="testing"),
            },
            areas=list(areas.values()),
            algo_version="fake-1",
            now=h.now,
        )
        backend = await session.get(Area, areas["backend"].id)
        qa = await session.get(Area, areas["qa"].id)
        assert backend is not None
        assert qa is not None
        backend.sort = 1000
        qa.archived_at = h.now
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/users/{member.id}/scores")
        assert response.status_code == 200
        assert response.json() == [
            {
                "area_id": str(areas[key].id),
                "area_key": key,
                "effective": value,
                "reason": reason,
                "calculated": value,
                "calc_reason": reason,
                "override": None,
                "override_by": None,
                "override_reason": None,
                "override_evidence": None,
                "override_at": None,
                "allowed_actions": [],
            }
            for key, value, reason in [("data", 3.0, "cv"), ("backend", 6.5, "quiz")]
        ]


@pytest.mark.parametrize("who", ["owner", "admin", "other"])
async def test_override_score_visibility(harness: Harness, who: str) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    admin = await h.admin()
    other = await h.user()
    async with h.db() as session:
        await write_calculated_scores(
            session,
            member.id,
            {"backend": ScoreWithReason(value=6.5, reason="quiz")},
            areas=list(areas.values()),
            algo_version="fake-1",
            now=h.now,
        )
        await set_score_override(
            session,
            h.jobs,
            member.id,
            "backend",
            9.0,
            "interview result",
            OverrideEvidence.INTERVIEW,
            admin.id,
            h.now,
        )
    actor = {"owner": member, "admin": admin, "other": other}[who]
    async with await h.client(actor) as client:
        response = await client.get(f"/api/v1/users/{member.id}/scores")
        assert response.status_code == 200
        assert len(response.json()) == 1
        data = response.json()[0]
        assert data["area_id"] == str(areas["backend"].id)
        assert data["area_key"] == "backend"
        assert data["effective"] == 9
        assert data["reason"] == "interview result"
        assert data["allowed_actions"] == (["override", "clear_override"] if who == "admin" else [])
        if who == "other":
            assert all(
                data[field] is None
                for field in (
                    "calculated",
                    "calc_reason",
                    "override",
                    "override_by",
                    "override_reason",
                    "override_evidence",
                    "override_at",
                )
            )
        else:
            score = schemas.MemberAreaScore.model_validate(data)
            assert score.calculated == 6.5
            assert score.calc_reason == "quiz"
            assert score.override == 9
            assert score.override_by == admin.id
            assert score.override_reason == "interview result"
            assert score.override_evidence == OverrideEvidence.INTERVIEW
            assert score.override_at == h.now


async def test_member_without_scores(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with await h.client(member) as client:
        response = await client.get(f"/api/v1/users/{member.id}/scores")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.parametrize("target", ["missing", "not_onboarded"])
@pytest.mark.parametrize("suffix", ["scores", "scores/history"])
async def test_score_reads_target_not_found(harness: Harness, target: str, suffix: str) -> None:
    h = harness
    actor = await h.user()
    user_id = uuid4() if target == "missing" else (await h.user(onboarded=False)).id
    async with await h.client(actor) as client:
        response = await client.get(f"/api/v1/users/{user_id}/{suffix}")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_score_history_pagination_filters_and_access(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    admin = await h.admin()
    other = await h.user()
    events = [
        AreaScoreEvent(
            user_id=member.id,
            area_id=areas["backend"].id,
            kind=AreaScoreEventKind.OVERRIDE_SET,
            old_effective=Decimal("4.00"),
            new_effective=Decimal("9.00"),
            reason="interview result",
            algo_version=None,
            actor_id=admin.id,
            created_at=h.now,
        ),
        AreaScoreEvent(
            user_id=member.id,
            area_id=areas["qa"].id,
            kind=AreaScoreEventKind.CALCULATED,
            old_effective=Decimal("0.00"),
            new_effective=Decimal("3.00"),
            reason="testing",
            algo_version="fake-2",
            actor_id=None,
            created_at=h.now - timedelta(hours=1),
        ),
        AreaScoreEvent(
            user_id=member.id,
            area_id=areas["backend"].id,
            kind=AreaScoreEventKind.CALCULATED,
            old_effective=Decimal("0.00"),
            new_effective=Decimal("4.00"),
            reason="quiz",
            algo_version="fake-1",
            actor_id=None,
            created_at=h.now - timedelta(hours=2),
        ),
    ]
    async with h.db() as session:
        session.add_all(events)
        session.add(
            AreaScoreEvent(
                user_id=other.id,
                area_id=areas["backend"].id,
                kind=AreaScoreEventKind.CALCULATED,
                old_effective=Decimal("0.00"),
                new_effective=Decimal("5.00"),
                reason="other member",
                algo_version="fake-1",
                actor_id=None,
                created_at=h.now,
            )
        )
        qa = await session.get(Area, areas["qa"].id)
        assert qa is not None
        qa.archived_at = h.now
    path = f"/api/v1/users/{member.id}/scores/history"
    expected = [schemas.AreaScoreEvent.model_validate(event) for event in events]
    async with await h.client(member) as client:
        response = await client.get(path, params={"limit": 2})
        assert response.status_code == 200
        first = schemas.AreaScoreEventPage.model_validate(response.json())
        assert first.items == expected[:2]
        assert first.next_cursor is not None
        assert response.json()["items"][0]["algo_version"] is None
        assert response.json()["items"][1]["algo_version"] == "fake-2"
        response = await client.get(path, params={"limit": 2, "cursor": first.next_cursor})
        assert response.status_code == 200
        second = schemas.AreaScoreEventPage.model_validate(response.json())
        assert second.items == expected[2:]
        assert second.next_cursor is None
        assert {event.id for event in first.items}.isdisjoint(event.id for event in second.items)
        response = await client.get(path, params={"area": "backend"})
        assert response.status_code == 200
        assert schemas.AreaScoreEventPage.model_validate(response.json()).items == [
            expected[0],
            expected[2],
        ]
        response = await client.get(path, params={"area": "qa"})
        assert response.status_code == 200
        assert schemas.AreaScoreEventPage.model_validate(response.json()).items == [expected[1]]
        response = await client.get(path, params={"area": "unknown"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
    async with await h.client(other) as client:
        assert_problem(await client.get(path), 403, ErrorCode.FORBIDDEN)
    async with await h.client(admin) as client:
        response = await client.get(path)
        assert response.status_code == 200
        assert schemas.AreaScoreEventPage.model_validate(response.json()).items == expected
    async with h.anonymous() as client:
        assert_problem(await client.get(path), 401, ErrorCode.UNAUTHENTICATED)
