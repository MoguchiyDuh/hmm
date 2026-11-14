from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from harness import Harness
from hmm_contracts.common import AuditAction, ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


class Scenario:
    def __init__(
        self,
        h: Harness,
        mentor: models.User,
        learner: models.User,
        other: models.User,
        admin: models.User,
    ) -> None:
        self.h = h
        self.mentor = mentor
        self.learner = learner
        self.other = other
        self.admin = admin

    async def mentorship(
        self,
        status: str = "active",
        outcome: str | None = None,
        *,
        mentor: models.User | None = None,
        learner: models.User | None = None,
        **fields: Any,
    ) -> UUID:
        h = self.h
        mentor = mentor or self.mentor
        learner = learner or self.learner
        async with h.db() as session:
            area_id = await session.scalar(
                select(models.Area.id).order_by(models.Area.sort).limit(1)
            )
            assert area_id is not None
            request = models.HelpRequest(
                learner_id=learner.id,
                area_id=area_id,
                topic="FastAPI auth",
                goal="ship a login flow",
                format="online",
                status="matched",
                expires_at=h.now + timedelta(days=14),
            )
            session.add(request)
            await session.flush()
            row = models.Mentorship(
                request_id=request.id,
                mentor_id=mentor.id,
                learner_id=learner.id,
                area_id=area_id,
                kind="mentoring",
                mentor_score_at_start=Decimal("5.0"),
                learner_score_at_start=Decimal("5.0"),
                status=status,
                outcome=outcome,
                started_at=h.now - timedelta(days=20),
                mentor_trust=fields.pop("mentor_trust", "self"),
                learner_trust=fields.pop("learner_trust", "self"),
                **fields,
            )
            session.add(row)
            await session.flush()
            return row.id


async def scenario(h: Harness) -> Scenario:
    await h.seed_areas()
    return Scenario(
        h,
        await h.user(login="mentor"),
        await h.user(login="learner"),
        await h.user(login="other"),
        await h.admin(),
    )


async def ban(h: Harness, admin: models.User, user: models.User) -> None:
    async with await h.client(admin) as c:
        response = await c.post(f"/api/v1/admin/users/{user.id}/ban", json={"reason": "spam"})
        assert response.status_code == 200, response.text


async def row(h: Harness, mentorship_id: UUID) -> models.Mentorship:
    async with h.db() as session:
        found = await session.get(models.Mentorship, mentorship_id)
        assert found is not None
        return found


async def windows(h: Harness, mentorship_id: UUID) -> int:
    async with h.db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.ReviewWindow)
            .where(
                models.ReviewWindow.subject_type == "mentorship",
                models.ReviewWindow.subject_id == mentorship_id,
            )
        )
        return int(count or 0)


async def closed_notices(h: Harness, *users: models.User) -> int:
    async with h.db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.Notification)
            .where(
                models.Notification.user_id.in_([user.id for user in users]),
                models.Notification.type == "mentorship_closed",
            )
        )
        return int(count or 0)


async def count(h: Harness, model: type[models.Confirmation] | type[models.AuditLog]) -> int:
    async with h.db() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def audits(h: Harness, action: AuditAction) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def recompute_jobs(h: Harness) -> list[str]:
    return [
        job["args"]["user_id"]
        for job in await h.pending_jobs("recompute_member")
        if job["status"] == "todo"
    ]


async def test_ban_mentor_cancels_active_mentorship(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship(mentor_trust="peer", learner_trust="self")
    confirmations = await count(h, models.Confirmation)

    await ban(h, s.admin, s.mentor)

    cancelled = await row(h, mentorship_id)
    assert cancelled.status == "cancelled"
    assert cancelled.outcome is None
    assert cancelled.closed_at is None
    assert cancelled.stalled_at is None
    assert cancelled.mentor_trust == "peer"
    assert cancelled.learner_trust == "self"
    assert await windows(h, mentorship_id) == 0
    assert await closed_notices(h, s.mentor, s.learner) == 0
    assert await count(h, models.Confirmation) == confirmations
    bans = await audits(h, AuditAction.USER_BAN)
    assert [audit.entity_id for audit in bans] == [str(s.mentor.id)]
    transitions = await audits(h, AuditAction.SYSTEM_TRANSITION)
    assert [audit for audit in transitions if audit.entity_type == "mentorship"] == []
    assert await recompute_jobs(h) == [str(s.learner.id)]


async def test_ban_learner_cancels_only_live_rows_and_keeps_counterpart_activity(
    harness: Harness,
) -> None:
    h = harness
    s = await scenario(h)
    active = await s.mentorship()
    closed_at = h.now - timedelta(days=3)
    closed = await s.mentorship(
        "closed", "reached", closed_at=closed_at, mentor_trust="peer", summary="done"
    )
    already = await s.mentorship("cancelled")
    computed_at = h.now - timedelta(days=1)
    async with h.db() as session:
        session.add(
            models.FactAward(
                fact_type="mentorship_side",
                fact_id=closed,
                user_id=s.mentor.id,
                xp=Decimal("12.5"),
                activity=Decimal("3.25"),
                reason="mentored",
                algo_version="v1",
                computed_at=computed_at,
            )
        )
        session.add(
            models.MemberMetric(
                user_id=s.mentor.id,
                xp=Decimal("40.5"),
                activity=Decimal("7.75"),
                algo_version="v1",
                input_hash="hash",
                computed_at=computed_at,
            )
        )

    await ban(h, s.admin, s.learner)

    assert (await row(h, active)).status == "cancelled"
    kept = await row(h, closed)
    assert kept.status == "closed"
    assert kept.outcome == "reached"
    assert kept.closed_at == closed_at
    assert kept.mentor_trust == "peer"
    assert kept.summary == "done"
    assert (await row(h, already)).status == "cancelled"
    async with h.db() as session:
        award = await session.get(models.FactAward, ("mentorship_side", closed, s.mentor.id))
        metric = await session.get(models.MemberMetric, s.mentor.id)
        assert award is not None
        assert metric is not None
        assert (award.xp, award.activity, award.computed_at) == (
            Decimal("12.5"),
            Decimal("3.25"),
            computed_at,
        )
        assert (metric.xp, metric.activity, metric.computed_at) == (
            Decimal("40.5"),
            Decimal("7.75"),
            computed_at,
        )
    assert await recompute_jobs(h) == [str(s.mentor.id)]


async def test_ban_cancels_stalled_and_closing_rows(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    proposed_at = h.now - timedelta(days=2)
    stalled = await s.mentorship("stalled", stalled_at=h.now - timedelta(days=5))
    closing = await s.mentorship("closing", learner=s.other, close_proposed_at=proposed_at)

    await ban(h, s.admin, s.mentor)

    first = await row(h, stalled)
    second = await row(h, closing)
    assert first.status == "cancelled"
    assert first.stalled_at is None
    assert second.status == "cancelled"
    assert second.stalled_at is None
    assert second.close_proposed_at == proposed_at
    assert await windows(h, stalled) == 0
    assert await windows(h, closing) == 0
    assert await closed_notices(h, s.mentor, s.learner, s.other) == 0
    assert sorted(await recompute_jobs(h)) == sorted([str(s.learner.id), str(s.other.id)])


async def test_second_ban_changes_nothing(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    await ban(h, s.admin, s.mentor)
    before = await row(h, mentorship_id)
    jobs = await recompute_jobs(h)
    audit_rows = await count(h, models.AuditLog)

    await ban(h, s.admin, s.mentor)

    after = await row(h, mentorship_id)
    assert (after.status, after.updated_at) == (before.status, before.updated_at)
    assert await recompute_jobs(h) == jobs
    assert await count(h, models.AuditLog) == audit_rows


async def test_unban_restores_nothing(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    await ban(h, s.admin, s.learner)
    jobs = await h.pending_jobs()

    async with await h.client(s.admin) as c:
        response = await c.post(f"/api/v1/admin/users/{s.learner.id}/unban")
        assert response.status_code == 200, response.text

    assert (await row(h, mentorship_id)).status == "cancelled"
    assert await h.pending_jobs() == jobs


async def test_matched_request_and_access(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()
    request_id = (await row(h, mentorship_id)).request_id

    async with await h.client(s.other) as c:
        response = await c.post(f"/api/v1/admin/users/{s.learner.id}/ban", json={"reason": "spam"})
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        response = await c.post(f"/api/v1/admin/users/{s.learner.id}/ban", json={"reason": "spam"})
        assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    assert (await row(h, mentorship_id)).status == "active"

    await ban(h, s.admin, s.learner)

    async with h.db() as session:
        request = await session.get(models.HelpRequest, request_id)
        assert request is not None
        assert request.status == "matched"
    assert (await row(h, mentorship_id)).status == "cancelled"


async def test_participant_cancel_opens_no_review_window(harness: Harness) -> None:
    h = harness
    s = await scenario(h)
    mentorship_id = await s.mentorship()

    async with await h.client(s.learner) as c:
        response = await c.post(f"/api/v1/mentorships/{mentorship_id}/cancel")
        assert response.status_code == 200, response.text

    assert await windows(h, mentorship_id) == 0
    assert await closed_notices(h, s.mentor, s.learner) == 0
