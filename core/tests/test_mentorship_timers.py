from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from harness import Harness
from hmm_core import models
from hmm_core.mentorship import timers
from sqlalchemy import func, select


async def setup(h: Harness) -> tuple[models.User, models.User]:
    await h.seed_areas()
    return await h.user(login="learner"), await h.user(login="mentor")


async def add_mentorship(
    h: Harness,
    mentor: models.User,
    learner: models.User,
    started_at: datetime,
    **fields: Any,
) -> UUID:
    async with h.db() as session:
        area_id = await session.scalar(select(models.Area.id).order_by(models.Area.sort).limit(1))
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
            status=fields.pop("status", "active"),
            started_at=started_at,
            last_checkin_at=fields.pop("last_checkin_at", None),
            mentor_trust="self",
            learner_trust="self",
            **fields,
        )
        session.add(row)
        await session.flush()
        return row.id


async def mentorship_row(h: Harness, mentorship_id: UUID) -> models.Mentorship:
    async with h.db() as session:
        row = await session.get(models.Mentorship, mentorship_id)
        assert row is not None
        return row


async def notices(h: Harness, user: models.User, type: str) -> list[dict[str, Any]]:
    async with h.db() as session:
        rows = await session.scalars(
            select(models.Notification)
            .where(models.Notification.user_id == user.id, models.Notification.type == type)
            .order_by(models.Notification.created_at, models.Notification.id)
        )
        return [cast(dict[str, Any], row.payload) for row in rows]


async def audits(h: Harness, mentorship_id: UUID) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.entity_type == "mentorship",
                    models.AuditLog.entity_id == str(mentorship_id),
                    models.AuditLog.action == "system_transition",
                )
            )
        )


async def window_count(h: Harness, mentorship_id: UUID) -> int:
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


async def idle(h: Harness) -> int:
    async with h.db() as session:
        return await timers.send_idle_warnings(session, h.jobs, h.now)


async def stall(h: Harness) -> int:
    async with h.db() as session:
        return await timers.stall_mentorships(session, h.jobs, h.now)


async def close_stalled(h: Harness) -> int:
    async with h.db() as session:
        return await timers.close_stalled_mentorships(session, h.jobs, h.now)


async def close_expired(h: Harness) -> int:
    async with h.db() as session:
        return await timers.close_expired_proposals(session, h.jobs, h.now)


async def test_idle_warning_threshold(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    started_at = h.now - timedelta(days=10)
    await add_mentorship(h, mentor, learner, started_at)
    assert await idle(h) == 1
    for user in (mentor, learner):
        payloads = await notices(h, user, "mentorship_idle")
        assert len(payloads) == 1
        assert payloads[0]["idle_days"] == 10
        assert datetime.fromisoformat(payloads[0]["stalls_at"]) == started_at + timedelta(days=14)
        assert [action["label_key"] for action in payloads[0]["actions"]] == [
            "notifications.actions.check_in"
        ]
    assert await idle(h) == 0
    assert len(await notices(h, mentor, "mentorship_idle")) == 1
    other_learner, other_mentor = await h.user(), await h.user()
    await add_mentorship(h, other_mentor, other_learner, h.now - timedelta(days=9))
    assert await idle(h) == 0
    assert await notices(h, other_mentor, "mentorship_idle") == []


async def test_stall(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner, h.now - timedelta(days=14))
    assert await stall(h) == 1
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "stalled"
    assert row.stalled_at == h.now
    for user in (mentor, learner):
        payloads = await notices(h, user, "mentorship_stalled")
        assert len(payloads) == 1
        assert payloads[0]["idle_days"] == 14
        assert datetime.fromisoformat(payloads[0]["closes_at"]) == h.now + timedelta(days=14)
    found = await audits(h, mentorship_id)
    assert len(found) == 1
    assert cast(dict[str, Any], found[0].after)["status"] == "stalled"
    assert len(await h.pending_jobs("recompute_member")) == 2
    assert await stall(h) == 0


async def test_inactivity_uses_last_checkin(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    await add_mentorship(
        h,
        mentor,
        learner,
        h.now - timedelta(days=40),
        last_checkin_at=h.now - timedelta(days=13),
    )
    assert await stall(h) == 0
    h.advance(timedelta(days=1))
    assert await stall(h) == 1


async def test_reactivation_restarts_clock(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner, h.now - timedelta(days=14))
    assert await stall(h) == 1
    async with await h.client(learner) as c:
        response = await c.post(
            f"/api/v1/mentorships/{mentorship_id}/checkins",
            json={"note": "back on track", "occurred_on": h.now.date().isoformat()},
        )
        assert response.status_code == 201, response.text
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "active"
    assert row.stalled_at is None
    before = len(await notices(h, learner, "mentorship_idle"))
    h.advance(timedelta(days=9))
    assert await idle(h) == 0
    h.advance(timedelta(days=1))
    assert await idle(h) == 1
    assert len(await notices(h, learner, "mentorship_idle")) == before + 1


async def test_stalled_auto_close(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner, h.now - timedelta(days=14))
    assert await stall(h) == 1
    h.advance(timedelta(days=13))
    assert await close_stalled(h) == 0
    h.advance(timedelta(days=1))
    assert await close_stalled(h) == 1
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "closed"
    assert row.outcome is None
    assert row.closed_at == h.now
    assert await window_count(h, mentorship_id) == 1
    for user in (mentor, learner):
        payloads = await notices(h, user, "mentorship_closed")
        assert len(payloads) == 1
        assert payloads[0]["outcome"] is None
        assert payloads[0]["actions"] == []
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.FeedItem)
                .where(
                    models.FeedItem.kind == "mentorship_completed",
                    models.FeedItem.subject_id == mentorship_id,
                )
            )
            == 0
        )
    assert [row.reason for row in await audits(h, mentorship_id) if row.reason] == [
        "stalled_auto_closed"
    ]
    assert await close_stalled(h) == 0


async def test_proposal_expiry(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner, h.now - timedelta(days=3))
    async with await h.client(mentor) as c:
        response = await c.post(f"/api/v1/mentorships/{mentorship_id}/propose-close")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "closing"
    h.advance(timedelta(days=6, hours=23))
    assert await close_expired(h) == 0
    h.advance(timedelta(hours=1))
    assert await close_expired(h) == 1
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "closed"
    assert row.outcome is None
    assert row.mentor_trust == "self"
    assert await window_count(h, mentorship_id) == 1
    async with h.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Confirmation)
                .where(models.Confirmation.fact_id == mentorship_id)
            )
            == 0
        )
    assert [row.reason for row in await audits(h, mentorship_id)] == ["close_proposal_expired"]
    assert await close_expired(h) == 0


async def test_run_mentorship_timers(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner, h.now - timedelta(days=20))
    async with h.db() as session:
        assert await timers.run_mentorship_timers(session, h.jobs, h.now) is None
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "stalled"
    assert row.stalled_at == h.now
    assert len(await notices(h, learner, "mentorship_idle")) == 1
    assert len(await notices(h, learner, "mentorship_stalled")) == 1
    assert await close_stalled(h) == 0


async def test_terminal_rows_ignored(harness: Harness) -> None:
    h = harness
    learner, mentor = await setup(h)
    old = h.now - timedelta(days=60)
    await add_mentorship(
        h,
        mentor,
        learner,
        old,
        status="cancelled",
        stalled_at=old,
        close_proposed_at=old,
    )
    await add_mentorship(
        h,
        mentor,
        await h.user(),
        old,
        status="closed",
        closed_at=old,
        stalled_at=old,
        close_proposed_at=old,
    )
    assert await idle(h) == 0
    assert await stall(h) == 0
    assert await close_stalled(h) == 0
    assert await close_expired(h) == 0
