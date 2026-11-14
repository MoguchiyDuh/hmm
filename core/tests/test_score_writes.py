from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import ScoreWithReason
from hmm_contracts.common import (
    ActorKind,
    AreaScoreEventKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
)
from hmm_contracts.notifications import NotificationType, ScoreChange, ScoreChangedPayload
from hmm_core.models import (
    Area,
    AreaScore,
    AreaScoreEvent,
    AuditLog,
    Notification,
    NotificationDelivery,
)
from hmm_core.scores.service import member_area_score, write_calculated_scores
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


async def test_calculated_score_writes(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    async with h.db() as session:
        changes = await write_calculated_scores(
            session,
            member.id,
            {"backend": ScoreWithReason(value=4.0, reason="seed")},
            areas=list(areas.values()),
            algo_version="fake-1",
            now=h.now,
        )
        assert changes == [
            ScoreChange(
                area_key="backend", area_name="Backend", previous_score=0, score=4, reason="seed"
            )
        ]
    async with h.db() as session:
        score = (await session.scalars(select(AreaScore))).one()
        assert score.user_id == member.id
        assert score.area_id == areas["backend"].id
        assert score.calculated == Decimal("4.00")
        assert score.calc_reason == "seed"
        event = (await session.scalars(select(AreaScoreEvent))).one()
        assert event.kind == AreaScoreEventKind.CALCULATED
        assert event.old_effective == Decimal("0.00")
        assert event.new_effective == Decimal("4.00")
        assert event.algo_version == "fake-1"
        assert event.actor_id is None
        assert event.reason == "seed"
        notification = (await session.scalars(select(Notification))).one()
        assert notification.user_id == member.id
        assert notification.type == NotificationType.SCORE_CHANGED
        assert notification.dedupe_key is None
        assert ScoreChangedPayload.model_validate(notification.payload) == ScoreChangedPayload(
            link=f"/users/{member.id}/scores", changes=tuple(changes)
        )
    scores = {"backend": ScoreWithReason(value=4.5, reason="updated")}
    async with h.db() as session:
        changes = await write_calculated_scores(
            session,
            member.id,
            scores,
            areas=list(areas.values()),
            algo_version="fake-1",
            now=h.now,
        )
        assert len(changes) == 1
        assert changes[0].previous_score == 4
        assert changes[0].score == 4.5
    async with h.db() as session:
        score = (await session.scalars(select(AreaScore))).one()
        assert score.calculated == Decimal("4.50")
        assert score.calc_reason == "updated"
        events = list(await session.scalars(select(AreaScoreEvent).order_by(AreaScoreEvent.id)))
        assert len(events) == 2
        assert events[-1].old_effective == Decimal("4.00")
        assert events[-1].new_effective == Decimal("4.50")
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1
    async with h.db() as session:
        assert (
            await write_calculated_scores(
                session,
                member.id,
                scores,
                areas=list(areas.values()),
                algo_version="fake-1",
                now=h.now,
            )
            == []
        )
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(AreaScoreEvent)) == 2
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_override_preservation_removal_and_visibility(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    admin = await h.admin()
    other = await h.user()
    await h.link_telegram(member)
    async with h.db() as session:
        assert (
            await write_calculated_scores(
                session,
                member.id,
                {"backend": ScoreWithReason(value=0.0, reason="seed")},
                areas=list(areas.values()),
                algo_version="fake-1",
                now=h.now,
            )
            == []
        )
    path = f"/api/v1/admin/users/{member.id}/scores/backend/override"
    body = {"value": 9.0, "reason": "interview result", "evidence": "interview"}
    async with await h.client(admin) as client:
        response = await client.put(path, json=body)
        assert response.status_code == 200
        data = response.json()
        assert data["area_id"] == str(areas["backend"].id)
        assert data["area_key"] == "backend"
        assert data["effective"] == 9
        assert data["reason"] == "interview result"
        assert data["calculated"] == 0
        assert data["override"] == 9
        assert data["override_reason"] == "interview result"
        assert data["override_evidence"] == "interview"
        assert data["allowed_actions"] == ["override", "clear_override"]
        async with h.db() as session:
            score = (await session.scalars(select(AreaScore))).one()
            assert score.override_by == admin.id
            assert score.override_at == h.now
            event = (await session.scalars(select(AreaScoreEvent))).one()
            assert event.kind == AreaScoreEventKind.OVERRIDE_SET
            assert event.algo_version is None
            assert event.old_effective == Decimal("0.00")
            assert event.new_effective == Decimal("9.00")
            assert event.actor_id == admin.id
            audit = (await session.scalars(select(AuditLog))).one()
            assert audit.action == AuditAction.SCORE_OVERRIDE_SET
            assert audit.actor_id == admin.id
            assert audit.actor_kind == ActorKind.ADMIN
            assert audit.entity_type == AuditEntityType.AREA_SCORE
            assert audit.entity_id == f"{member.id}:{areas['backend'].id}"
            assert audit.before == {"calculated": 0.0, "override": None, "effective": 0.0}
            assert audit.after == {"calculated": 0.0, "override": 9.0, "effective": 9.0}
            assert audit.reason == "interview result"
            notification = (await session.scalars(select(Notification))).one()
            assert notification.user_id == member.id
            assert notification.type == NotificationType.SCORE_CHANGED
            payload = ScoreChangedPayload.model_validate(notification.payload)
            assert payload.changes == (
                ScoreChange(
                    area_key="backend",
                    area_name="Backend",
                    previous_score=0,
                    score=9,
                    reason="interview result",
                ),
            )
            assert await session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
            owner_view = member_area_score(areas["backend"], score, actor=member)
            assert owner_view.calculated == 0
            assert owner_view.override == 9
            assert owner_view.override_by == admin.id
            assert owner_view.allowed_actions == []
            other_view = member_area_score(areas["backend"], score, actor=other)
            assert other_view.effective == 9
            assert other_view.reason == "interview result"
            assert other_view.calculated is None
            assert other_view.calc_reason is None
            assert other_view.override is None
            assert other_view.override_by is None
            assert other_view.override_reason is None
            assert other_view.override_evidence is None
            assert other_view.override_at is None
            assert other_view.allowed_actions == []
        jobs = await h.pending_jobs("recompute_member")
        assert len(jobs) == 1
        assert jobs[0]["args"] == {"user_id": str(member.id)}
        assert jobs[0]["lock"] == f"member:{member.id}"
        assert jobs[0]["queueing_lock"] == f"recompute_member:{member.id}"
        h.advance(timedelta(minutes=1))
        assert (await client.put(path, json=body)).json() == data
        assert await h.pending_jobs("recompute_member") == jobs
        async with h.db() as session:
            assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1
            assert await session.scalar(select(func.count()).select_from(AreaScoreEvent)) == 1
            assert await session.scalar(select(func.count()).select_from(Notification)) == 1
            assert (
                await write_calculated_scores(
                    session,
                    member.id,
                    {"backend": ScoreWithReason(value=5.0, reason="recomputed")},
                    areas=list(areas.values()),
                    algo_version="fake-1",
                    now=h.now,
                )
                == []
            )
        async with h.db() as session:
            score = (await session.scalars(select(AreaScore))).one()
            assert score.override == Decimal("9.00")
            assert score.calculated == Decimal("5.00")
            assert score.calc_reason == "recomputed"
            assert score.override_reason == "interview result"
            assert await session.scalar(select(func.count()).select_from(AreaScoreEvent)) == 1
            assert await session.scalar(select(func.count()).select_from(Notification)) == 1
        response = await client.request("DELETE", path, json={"reason": "recheck"})
        assert response.status_code == 200
        data = response.json()
        assert data["override"] is None
        assert data["override_by"] is None
        assert data["override_reason"] is None
        assert data["override_evidence"] is None
        assert data["override_at"] is None
        assert data["effective"] == 5
        assert data["reason"] == "recomputed"
        assert data["allowed_actions"] == ["override"]
        async with h.db() as session:
            event = (
                await session.scalars(
                    select(AreaScoreEvent).where(
                        AreaScoreEvent.kind == AreaScoreEventKind.OVERRIDE_REMOVED
                    )
                )
            ).one()
            assert event.old_effective == Decimal("9.00")
            assert event.new_effective == Decimal("5.00")
            assert event.algo_version is None
            assert event.actor_id == admin.id
            assert event.reason == "recheck"
            audit = (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == AuditAction.SCORE_OVERRIDE_REMOVE)
                )
            ).one()
            assert audit.before == {"calculated": 5.0, "override": 9.0, "effective": 9.0}
            assert audit.after == {"calculated": 5.0, "override": None, "effective": 5.0}
            assert audit.reason == "recheck"
            notifications = list(
                await session.scalars(select(Notification).order_by(Notification.id))
            )
            assert len(notifications) == 2
            assert ScoreChangedPayload.model_validate(notifications[-1].payload).changes == (
                ScoreChange(
                    area_key="backend",
                    area_name="Backend",
                    previous_score=9,
                    score=5,
                    reason="recheck",
                ),
            )
            assert await session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
        jobs = await h.pending_jobs("recompute_member")
        assert jobs
        assert jobs[0]["args"] == {"user_id": str(member.id)}
        response = await client.delete(path)
        assert response.status_code == 200
        assert response.json() == data
        assert await h.pending_jobs("recompute_member") == jobs
        async with h.db() as session:
            assert await session.scalar(select(func.count()).select_from(AreaScoreEvent)) == 2
            assert await session.scalar(select(func.count()).select_from(AuditLog)) == 2
            assert await session.scalar(select(func.count()).select_from(Notification)) == 2


async def test_calculated_scores_filter_areas_and_batch_notifications(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    async with h.db() as session:
        await write_calculated_scores(
            session,
            member.id,
            {"qa": ScoreWithReason(value=0, reason="seed")},
            areas=list(areas.values()),
            algo_version="fake-1",
            now=h.now,
        )
        qa = await session.get(Area, areas["qa"].id)
        assert qa is not None
        qa.archived_at = h.now
        areas["qa"] = qa
    async with h.db() as session:
        changes = await write_calculated_scores(
            session,
            member.id,
            {
                "data": ScoreWithReason(value=2.0, reason="cv"),
                "backend": ScoreWithReason(value=1.0, reason="quiz"),
                "design": ScoreWithReason(value=0.5, reason="hint"),
                "qa": ScoreWithReason(value=9.0, reason="archived"),
                "unknown": ScoreWithReason(value=9.0, reason="unknown"),
            },
            areas=list(reversed(areas.values())),
            algo_version="fake-1",
            now=h.now,
        )
        assert [change.area_key for change in changes] == ["backend", "data", "design"]
    async with h.db() as session:
        rows = list(await session.scalars(select(AreaScore)))
        assert len(rows) == 4
        qa_score = next(row for row in rows if row.area_id == areas["qa"].id)
        assert qa_score.calculated == Decimal("0.00")
        assert qa_score.calc_reason == "seed"
        notification = (await session.scalars(select(Notification))).one()
        payload = ScoreChangedPayload.model_validate(notification.payload)
        assert [change.area_key for change in payload.changes] == ["backend", "data"]


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("non_admin", 403, ErrorCode.FORBIDDEN),
        ("missing_user", 404, ErrorCode.NOT_FOUND),
        ("unknown_area", 404, ErrorCode.NOT_FOUND),
        ("archived_area", 409, ErrorCode.INVALID_STATE),
        ("missing_score", 409, ErrorCode.INVALID_STATE),
        ("not_onboarded", 404, ErrorCode.NOT_FOUND),
    ],
)
async def test_override_errors(harness: Harness, case: str, status: int, code: ErrorCode) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user(onboarded=case != "not_onboarded")
    actor = await h.user(admin=case != "non_admin")
    async with h.db() as session:
        if case == "archived_area":
            area = await session.get(Area, areas["qa"].id)
            assert area is not None
            area.archived_at = h.now
    user_id = uuid4() if case == "missing_user" else member.id
    area_key = "unknown" if case == "unknown_area" else "qa"
    async with await h.client(actor) as client:
        response = await client.put(
            f"/api/v1/admin/users/{user_id}/scores/{area_key}/override",
            json={"value": 9, "reason": "interview result", "evidence": "interview"},
        )
        assert_problem(response, status, code)
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(AreaScoreEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0
    assert await h.pending_jobs("recompute_member") == []
