import dataclasses
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts import algo
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AreaScoreEventKind,
    AttendanceRole,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    FlagStatus,
    FlagSubjectType,
    ReviewContext,
    Severity,
    Trust,
)
from hmm_contracts.notifications import AlertKind, AlertStatus, NotificationType
from hmm_core import models
from hmm_core.onboarding.baseline import save_onboarding_baseline
from hmm_core.platform.jobs import REGISTRY, JobName
from hmm_core.scores import recompute
from hmm_core.scores.jobs import recompute_member as recompute_member_job
from hmm_core.scores.schemas import MemberMetrics
from sqlalchemy import delete, func, select
from test_api_skeleton import assert_problem


class FlaggingAlgorithm(FakeAlgorithm):
    def __init__(self, flags: Sequence[algo.Flag]) -> None:
        self.flags = list(flags)

    async def compute_member(
        self, ctx: algo.AlgoContext, member: algo.MemberHistory, areas: list[algo.Area]
    ) -> algo.MemberMetrics:
        metrics = await super().compute_member(ctx, member, areas)
        return metrics.model_copy(update={"flags": list(self.flags)})


class BrokenAlgorithm(FakeAlgorithm):
    async def compute_member(
        self, ctx: algo.AlgoContext, member: algo.MemberHistory, areas: list[algo.Area]
    ) -> algo.MemberMetrics:
        return algo.MemberMetrics(area_scores={}, xp=0, activity=0, fact_awards=[], flags=[])


def algo_flag(
    subject_type: FlagSubjectType,
    subject_id: UUID,
    reason: str,
    severity: Severity = Severity.LOW,
    related: Sequence[UUID] = (),
) -> algo.Flag:
    return algo.Flag.model_validate(
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "related_user_ids": list(related),
            "reason": reason,
            "severity": severity,
        }
    )


async def seed_attendance(
    h: Harness, user_id: UUID, areas: dict[str, models.Area]
) -> models.EventAttendance:
    async with h.db() as session:
        event = models.Event(
            slug=f"event-{uuid4().hex[:8]}",
            kind="hackathon",
            formation_enabled=True,
            title="Hackathon",
            starts_at=h.now - timedelta(days=3),
            ends_at=h.now - timedelta(days=2),
            timezone="UTC",
            format="offline",
            language=[],
            themes=[],
            status="published",
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        attendance = models.EventAttendance(
            event_id=event.id,
            user_id=user_id,
            status=AttendanceStatus.ATTENDED,
            role=AttendanceRole.ATTENDEE,
            proof_links=[],
            trust=Trust.TRUSTED,
            status_at=h.now,
        )
        session.add_all(
            [
                models.EventArea(
                    event_id=event.id,
                    area_id=areas["backend"].id,
                    importance=AreaImportance.PRIMARY,
                    headcount=None,
                ),
                attendance,
            ]
        )
        await session.flush()
        return attendance


async def seed_baseline(h: Harness, user_id: UUID, value: float) -> None:
    async with h.db() as session:
        await save_onboarding_baseline(
            session,
            user_id,
            algo.OnboardingBaseline(
                submitted_at=h.now,
                area_scores={"backend": algo.ScoreWithReason(value=value, reason="quiz")},
                discrepancies=[],
            ),
        )


async def run(h: Harness, user_id: UUID) -> bool:
    async with h.db() as session:
        return await recompute.recompute_member(session, h.runtime, user_id)


async def count(h: Harness, model: type[models.Base]) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def forget_metrics(h: Harness, user_id: UUID) -> None:
    async with h.db() as session:
        await session.execute(
            delete(models.MemberMetric).where(models.MemberMetric.user_id == user_id)
        )


async def live_alerts(h: Harness) -> list[models.AdminAlert]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AdminAlert).where(
                    models.AdminAlert.type == AlertKind.SUSPICIOUS_ACTIVITY,
                    models.AdminAlert.status == AlertStatus.OPEN,
                )
            )
        )


async def test_recompute_happy_path_and_noop(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    await seed_baseline(h, member.id, 4.0)
    attendance = await seed_attendance(h, member.id, areas)
    assert await run(h, member.id) is True
    async with h.db() as session:
        score = await session.scalar(
            select(models.AreaScore).where(
                models.AreaScore.user_id == member.id,
                models.AreaScore.area_id == areas["backend"].id,
            )
        )
        assert score is not None
        assert score.calculated == Decimal("4.10")
        events = list(await session.scalars(select(models.AreaScoreEvent)))
        assert [(event.kind, event.area_id) for event in events] == [
            (AreaScoreEventKind.CALCULATED, areas["backend"].id)
        ]
        metric = (await session.scalars(select(models.MemberMetric))).one()
        assert metric.user_id == member.id
        assert metric.algo_version == "fake-1"
        assert len(metric.input_hash) == 64
        assert metric.xp == Decimal("10.0")
        awards = list(await session.scalars(select(models.FactAward)))
        assert [(award.fact_type, award.fact_id, award.user_id) for award in awards] == [
            ("attendance", attendance.id, member.id)
        ]
        assert awards[0].algo_version == "fake-1"
        assert awards[0].computed_at == h.now
    pending = await h.pending_jobs("compute_lead_fits_for_member")
    assert [row["args"] for row in pending] == [{"user_id": str(member.id)}]
    events_before = await count(h, models.AreaScoreEvent)
    notifications_before = await count(h, models.Notification)
    async with h.db() as session:
        computed_at = await session.scalar(select(models.MemberMetric.updated_at))
        award_ids = list(await session.scalars(select(models.FactAward.created_at)))
    h.advance(timedelta(minutes=5))
    assert await run(h, member.id) is False
    assert await count(h, models.AreaScoreEvent) == events_before
    assert await count(h, models.Notification) == notifications_before
    async with h.db() as session:
        assert await session.scalar(select(models.MemberMetric.updated_at)) == computed_at
        assert await session.scalar(select(models.MemberMetric.computed_at)) == h.now - timedelta(
            minutes=5
        )
        assert list(await session.scalars(select(models.FactAward.created_at))) == award_ids
    assert len(await h.pending_jobs("compute_lead_fits_for_member")) == 1


async def test_recompute_replaces_awards(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    attendance = await seed_attendance(h, member.id, areas)
    assert await run(h, member.id) is True
    async with h.db() as session:
        session.add(
            models.Flag(
                user_id=member.id,
                subject_type=FlagSubjectType.ATTENDANCE,
                subject_id=attendance.id,
                related_user_ids=[],
                reason="Fake proof",
                severity=Severity.HIGH,
                status=FlagStatus.PUNISHED,
                algo_version="fake-1",
                penalty={"exclude": True},
            )
        )
    assert await run(h, member.id) is True
    async with h.db() as session:
        metric = (await session.scalars(select(models.MemberMetric))).one()
        assert metric.xp == 0
        awards = list(await session.scalars(select(models.FactAward)))
        assert [(award.fact_id, award.xp) for award in awards] == [(attendance.id, 0)]
        await session.execute(
            delete(models.EventAttendance).where(models.EventAttendance.id == attendance.id)
        )
    assert await run(h, member.id) is True
    assert await count(h, models.FactAward) == 0
    async with h.db() as session:
        assert (await session.scalars(select(models.MemberMetric))).one().xp == 0


async def test_adjustments_apply_once(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    admin = await h.admin()
    await seed_attendance(h, member.id, areas)
    async with h.db() as session:
        session.add_all(
            [
                models.XpAdjustment(user_id=member.id, amount=5, reason="a", admin_id=admin.id),
                models.XpAdjustment(user_id=member.id, amount=-2, reason="b", admin_id=admin.id),
            ]
        )
    for _ in range(2):
        await forget_metrics(h, member.id)
        assert await run(h, member.id) is True
        async with h.db() as session:
            assert (await session.scalars(select(models.MemberMetric))).one().xp == 10
        async with await h.client(member) as client:
            response = await client.get(f"/api/v1/users/{member.id}/metrics")
        assert response.status_code == 200
        assert MemberMetrics.model_validate(response.json()).xp == 13.0


async def test_flag_persistence(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    other = await h.user()
    first_subject = uuid4()
    second_subject = uuid4()
    async with h.db() as session:
        pardoned = models.Flag(
            user_id=member.id,
            subject_type=FlagSubjectType.PROFILE,
            subject_id=first_subject,
            related_user_ids=[],
            reason="Old",
            severity=Severity.LOW,
            status=FlagStatus.PARDONED,
            algo_version="fake-0",
        )
        session.add(pardoned)
    h.runtime = dataclasses.replace(
        h.runtime,
        algorithm=FlaggingAlgorithm(
            [
                algo_flag(FlagSubjectType.PROFILE, first_subject, "Burst", related=[other.id]),
                algo_flag(FlagSubjectType.PROFILE, first_subject, "Burst"),
                algo_flag(FlagSubjectType.RING, second_subject, "Ring", Severity.MEDIUM),
            ]
        ),
    )
    assert await run(h, member.id) is True
    async with h.db() as session:
        live = {
            row.subject_id: row
            for row in await session.scalars(
                select(models.Flag).where(models.Flag.status == FlagStatus.TO_CHECK)
            )
        }
        assert set(live) == {first_subject, second_subject}
        assert live[first_subject].id != pardoned.id
        assert live[first_subject].user_id == member.id
        assert live[first_subject].related_user_ids == []
        assert live[first_subject].algo_version == "fake-1"
        assert live[first_subject].penalty is None
        restored = await session.get(models.Flag, pardoned.id)
        assert restored is not None
        assert restored.status == FlagStatus.PARDONED
        assert restored.reason == "Old"
    alerts = await live_alerts(h)
    assert len(alerts) == 1
    assert alerts[0].dedupe_key is not None
    assert alerts[0].dedupe_key.endswith(f"{member.id}:{h.now.date().isoformat()}")
    assert alerts[0].subject_id == member.id
    await forget_metrics(h, member.id)
    h.runtime = dataclasses.replace(
        h.runtime,
        algorithm=FlaggingAlgorithm(
            [
                algo_flag(
                    FlagSubjectType.PROFILE,
                    first_subject,
                    "Second reason",
                    Severity.HIGH,
                    related=[other.id],
                ),
                algo_flag(FlagSubjectType.RING, second_subject, "Ring", Severity.LOW),
            ]
        ),
    )
    assert await run(h, member.id) is True
    async with h.db() as session:
        merged = await session.scalar(
            select(models.Flag).where(
                models.Flag.subject_id == first_subject,
                models.Flag.status == FlagStatus.TO_CHECK,
            )
        )
        assert merged is not None
        assert merged.reason == "Burst\nSecond reason"
        assert merged.severity == Severity.HIGH
        assert merged.related_user_ids == [other.id]
        ring = await session.scalar(
            select(models.Flag).where(models.Flag.subject_id == second_subject)
        )
        assert ring is not None
        assert ring.reason == "Ring"
        assert ring.severity == Severity.MEDIUM
    assert len(await live_alerts(h)) == 1
    await forget_metrics(h, member.id)
    h.runtime = dataclasses.replace(h.runtime, algorithm=FlaggingAlgorithm([]))
    assert await run(h, member.id) is True
    async with h.db() as session:
        statuses = list(
            await session.scalars(
                select(models.Flag.status).where(models.Flag.user_id == member.id)
            )
        )
        assert sorted(statuses) == [FlagStatus.PARDONED, FlagStatus.TO_CHECK, FlagStatus.TO_CHECK]
    assert len(await live_alerts(h)) == 1


async def test_flag_through_job(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    attendance = await seed_attendance(h, member.id, areas)
    h.runtime = dataclasses.replace(
        h.runtime,
        algorithm=FlaggingAlgorithm(
            [algo_flag(FlagSubjectType.ATTENDANCE, attendance.id, "Unverified proof")]
        ),
    )
    await recompute_member_job(h.runtime, user_id=str(member.id))
    async with h.db() as session:
        flag = (await session.scalars(select(models.Flag))).one()
        assert flag.status == FlagStatus.TO_CHECK
        assert flag.subject_type == FlagSubjectType.ATTENDANCE
        assert flag.subject_id == attendance.id
        assert flag.algo_version == "fake-1"
    assert len(await live_alerts(h)) == 1


async def test_invalid_output_writes_nothing(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    await seed_attendance(h, member.id, areas)
    h.runtime = dataclasses.replace(h.runtime, algorithm=BrokenAlgorithm())
    with pytest.raises(ValueError):
        await run(h, member.id)
    assert await count(h, models.MemberMetric) == 0
    assert await count(h, models.FactAward) == 0
    assert await count(h, models.AreaScoreEvent) == 0
    assert await h.pending_jobs("compute_lead_fits_for_member") == []


async def test_score_change_notifies(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=member.id,
                area_id=areas["backend"].id,
                calculated=Decimal("5.00"),
                calc_reason="seed",
            )
        )
    assert await run(h, member.id) is True
    async with h.db() as session:
        event = (await session.scalars(select(models.AreaScoreEvent))).one()
        assert event.area_id == areas["backend"].id
        assert event.old_effective == Decimal("5.00")
        assert event.new_effective == Decimal("0.00")
        notification = (await session.scalars(select(models.Notification))).one()
        assert notification.user_id == member.id
        assert notification.type == NotificationType.SCORE_CHANGED
        assert isinstance(notification.payload, dict)
        changes = notification.payload["changes"]
        assert isinstance(changes, list)
        assert len(changes) == 1


async def test_recompute_all_members(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    first = await h.user()
    second = await h.user()
    await h.user(onboarded=False)
    await h.user(banned_at=h.now)
    async with h.db() as session:
        assert await recompute.recompute_all_members(session, h.runtime) == 2
    rows = await h.pending_jobs("recompute_member")
    assert sorted(row["lock"] for row in rows) == sorted(
        [f"member:{first.id}", f"member:{second.id}"]
    )
    async with h.db() as session:
        assert await recompute.recompute_all_members(session, h.runtime) == 0
    assert len(await h.pending_jobs("recompute_member")) == 2


async def test_admin_recompute_endpoint(harness: Harness) -> None:
    h = harness
    member = await h.user()
    admin = await h.admin()
    path = f"/api/v1/admin/users/{member.id}/recompute"
    async with await h.client(admin) as client:
        response = await client.post(path)
        assert response.status_code == 202
        assert_problem(
            await client.post(f"/api/v1/admin/users/{uuid4()}/recompute"), 404, ErrorCode.NOT_FOUND
        )
    async with h.db() as session:
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id == admin.id
        assert audit.actor_kind == ActorKind.ADMIN
        assert audit.action == AuditAction.MEMBER_RECOMPUTE
        assert audit.entity_type == AuditEntityType.USER
        assert audit.entity_id == str(member.id)
        assert audit.reason is None
    rows = await h.pending_jobs("recompute_member")
    assert [row["args"] for row in rows] == [{"user_id": str(member.id)}]
    async with await h.client(member) as client:
        assert_problem(await client.post(path), 403, ErrorCode.FORBIDDEN)
    other = await h.user()
    async with await h.client(admin) as client:
        response = await client.post(
            f"/api/v1/admin/users/{other.id}/recompute", json={"reason": "manual"}
        )
        assert response.status_code == 202
    async with h.db() as session:
        reasons = list(
            await session.scalars(
                select(models.AuditLog.reason).where(models.AuditLog.entity_id == str(other.id))
            )
        )
        assert reasons == ["manual"]
    assert len(await h.pending_jobs("recompute_member")) == 2


async def test_metrics_endpoint(harness: Harness) -> None:
    h = harness
    member = await h.user()
    other = await h.user()
    admin = await h.admin()
    pending = await h.user(onboarded=False)
    path = f"/api/v1/users/{member.id}/metrics"
    for caller in (member, other, admin):
        async with await h.client(caller) as client:
            response = await client.get(path)
        assert response.status_code == 200
        metrics = MemberMetrics.model_validate(response.json())
        assert metrics.xp == 0.0
        assert metrics.activity == 0.0
        assert set(metrics.reputation) == {
            ReviewContext.MEMBER,
            ReviewContext.LEAD,
            ReviewContext.MENTOR,
            ReviewContext.HOST,
        }
        assert metrics.hackathons_finished == 0
        assert metrics.projects_shipped == 0
        assert metrics.mentorships_completed == 0
    async with await h.client(other) as client:
        assert_problem(
            await client.get(f"/api/v1/users/{uuid4()}/metrics"), 404, ErrorCode.NOT_FOUND
        )
        assert_problem(
            await client.get(f"/api/v1/users/{pending.id}/metrics"), 404, ErrorCode.NOT_FOUND
        )
    async with h.anonymous() as client:
        assert_problem(await client.get(path), 401, ErrorCode.UNAUTHENTICATED)


async def test_jobs_registered(harness: Harness) -> None:
    assert harness.jobs is not None
    assert JobName.RECOMPUTE_MEMBER in REGISTRY
    assert JobName.RECOMPUTE_ALL_MEMBERS in REGISTRY
