from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from hmm_core.attendance.service import ensure_attended, expire_going_attendances
from hmm_core.platform.errors import ProblemError
from sqlalchemy import func, select


async def seed_event(harness: Harness, *, ends_at: datetime | None = None) -> UUID:
    end = ends_at or harness.now + timedelta(days=3)
    async with harness.db() as session:
        event = models.Event(
            slug=str(uuid4()),
            kind="hackathon",
            formation_enabled=False,
            title="Event",
            starts_at=end - timedelta(days=1),
            ends_at=end,
            timezone="UTC",
            language=[],
            themes=[],
            status="published",
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        return event.id


async def seed_attendance(
    harness: Harness,
    event_id: UUID,
    user_id: UUID,
    status: str,
    *,
    role: str = "attendee",
    proof_links: list[str] | None = None,
) -> UUID:
    async with harness.db() as session:
        row = models.EventAttendance(
            event_id=event_id,
            user_id=user_id,
            status=status,
            role=role,
            proof_links=proof_links or [],
            note=None,
            trust="self",
            status_at=harness.now - timedelta(days=40),
        )
        session.add(row)
        await session.flush()
        return row.id


async def attendance_of(harness: Harness, event_id: UUID, user_id: UUID) -> models.EventAttendance:
    async with harness.db() as session:
        row = await session.scalar(
            select(models.EventAttendance).where(
                models.EventAttendance.event_id == event_id,
                models.EventAttendance.user_id == user_id,
            )
        )
        assert row is not None
        return row


async def count(harness: Harness, model: type[models.Base]) -> int:
    async with harness.db() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return value or 0


async def system_confirmations(harness: Harness) -> list[models.Confirmation]:
    async with harness.db() as session:
        return list(
            await session.scalars(
                select(models.Confirmation).where(models.Confirmation.withdrawn_at.is_(None))
            )
        )


async def test_ensure_attended_is_idempotent_with_one_system_confirmation(harness: Harness) -> None:
    event_id = await seed_event(harness)
    member = await harness.user(login="member")
    await harness.user(login="other")
    for _ in range(2):
        async with harness.db() as session:
            await ensure_attended(
                session, harness.jobs, event_id=event_id, user_id=member.id, now=harness.now
            )
    async with harness.db() as session:
        rows = list(await session.scalars(select(models.EventAttendance)))
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == member.id
    assert row.status == "attended"
    assert row.role == "attendee"
    assert row.trust == "trusted"
    assert row.status_at == harness.now
    confirmations = await system_confirmations(harness)
    assert len(confirmations) == 1
    assert confirmations[0].source == "system"
    assert confirmations[0].confirmer_id is None
    assert confirmations[0].fact_type == "attendance"
    assert confirmations[0].fact_id == row.id
    assert len(await harness.pending_jobs("recompute_member")) == 1


async def test_ensure_attended_transitions_existing_rows(harness: Harness) -> None:
    event_id = await seed_event(harness)
    going = await harness.user(login="going")
    attended = await harness.user(login="attended")
    cancelled = await harness.user(login="cancelled")
    await seed_attendance(harness, event_id, going.id, "going")
    await seed_attendance(
        harness,
        event_id,
        attended.id,
        "attended",
        role="speaker",
        proof_links=["https://x.test/1"],
    )
    await seed_attendance(harness, event_id, cancelled.id, "cancelled")
    for user in (going, attended, cancelled):
        async with harness.db() as session:
            await ensure_attended(
                session, harness.jobs, event_id=event_id, user_id=user.id, now=harness.now
            )
    going_row = await attendance_of(harness, event_id, going.id)
    assert going_row.status == "attended"
    assert going_row.status_at == harness.now
    attended_row = await attendance_of(harness, event_id, attended.id)
    assert attended_row.status == "attended"
    assert attended_row.role == "speaker"
    assert attended_row.proof_links == ["https://x.test/1"]
    assert attended_row.status_at == harness.now - timedelta(days=40)
    cancelled_row = await attendance_of(harness, event_id, cancelled.id)
    assert cancelled_row.status == "attended"
    assert cancelled_row.status_at == harness.now


async def test_ensure_attended_without_system_confirmation(harness: Harness) -> None:
    event_id = await seed_event(harness)
    member = await harness.user(login="member")
    for _ in range(2):
        async with harness.db() as session:
            await ensure_attended(
                session,
                harness.jobs,
                event_id=event_id,
                user_id=member.id,
                now=harness.now,
                system_confirmation=False,
            )
        assert len(await harness.pending_jobs("recompute_member")) == 1
        assert await count(harness, models.EventAttendance) == 1
    row = await attendance_of(harness, event_id, member.id)
    assert row.status == "attended"
    assert row.trust == "self"
    assert await count(harness, models.Confirmation) == 0


async def test_ensure_attended_has_no_window_check(harness: Harness) -> None:
    member = await harness.user(login="member")
    async with harness.db() as session:
        with pytest.raises(ProblemError) as error:
            await ensure_attended(
                session, harness.jobs, event_id=uuid4(), user_id=member.id, now=harness.now
            )
    assert error.value.code == ErrorCode.NOT_FOUND
    future = await seed_event(harness, ends_at=harness.now + timedelta(days=10))
    ended = await seed_event(harness, ends_at=harness.now - timedelta(days=90))
    for event_id in (future, ended):
        async with harness.db() as session:
            await ensure_attended(
                session, harness.jobs, event_id=event_id, user_id=member.id, now=harness.now
            )
        assert (await attendance_of(harness, event_id, member.id)).status == "attended"


async def test_result_participants_shape(harness: Harness) -> None:
    event_id = await seed_event(harness, ends_at=harness.now - timedelta(hours=1))
    first = await harness.user(login="first")
    second = await harness.user(login="second")
    third = await harness.user(login="third")
    for user in (first, second, first):
        async with harness.db() as session:
            await ensure_attended(
                session, harness.jobs, event_id=event_id, user_id=user.id, now=harness.now
            )
    async with harness.db() as session:
        users = set(await session.scalars(select(models.EventAttendance.user_id)))
    assert users == {first.id, second.id}
    confirmations = await system_confirmations(harness)
    assert len(confirmations) == 2
    assert {row.subject_user_id for row in confirmations} == {first.id, second.id}
    assert all(row.source == "system" for row in confirmations)
    jobs = await harness.pending_jobs("recompute_member")
    assert sorted(job["args"]["user_id"] for job in jobs) == sorted([str(first.id), str(second.id)])
    assert str(third.id) not in {job["args"]["user_id"] for job in jobs}


async def test_expire_going_attendances(harness: Harness) -> None:
    member = await harness.user(login="member")
    event_a = await seed_event(harness, ends_at=harness.now - timedelta(days=31))
    event_b = await seed_event(harness, ends_at=harness.now - timedelta(days=30))
    event_c = await seed_event(harness, ends_at=harness.now - timedelta(days=31))
    await seed_attendance(harness, event_a, member.id, "going")
    await seed_attendance(harness, event_b, member.id, "going")
    await seed_attendance(harness, event_c, member.id, "attended")
    notifications = await count(harness, models.Notification)
    audits = await count(harness, models.AuditLog)
    async with harness.db() as session:
        assert await expire_going_attendances(session, harness.now) == 1
    row_a = await attendance_of(harness, event_a, member.id)
    assert row_a.status == "cancelled"
    assert row_a.status_at == harness.now
    row_b = await attendance_of(harness, event_b, member.id)
    assert row_b.status == "going"
    assert row_b.status_at == harness.now - timedelta(days=40)
    row_c = await attendance_of(harness, event_c, member.id)
    assert row_c.status == "attended"
    assert row_c.status_at == harness.now - timedelta(days=40)
    async with harness.db() as session:
        assert await expire_going_attendances(session, harness.now) == 0
    assert await harness.pending_jobs("recompute_member") == []
    assert await count(harness, models.Notification) == notifications
    assert await count(harness, models.AuditLog) == audits


async def test_expire_going_attendances_limit(harness: Harness) -> None:
    member = await harness.user(login="member")
    for days in (31, 32):
        event_id = await seed_event(harness, ends_at=harness.now - timedelta(days=days))
        await seed_attendance(harness, event_id, member.id, "going")
    for expected in (1, 1, 0):
        async with harness.db() as session:
            assert await expire_going_attendances(session, harness.now, limit=1) == expected


async def test_expire_going_attendances_nothing_due(harness: Harness) -> None:
    member = await harness.user(login="member")
    recent = await seed_event(harness, ends_at=harness.now - timedelta(days=5))
    old = await seed_event(harness, ends_at=harness.now - timedelta(days=60))
    await seed_attendance(harness, recent, member.id, "going")
    await seed_attendance(harness, old, member.id, "attended")
    async with harness.db() as session:
        before = {
            row.id: row.updated_at
            for row in await session.execute(
                select(models.EventAttendance.id, models.EventAttendance.updated_at)
            )
        }
    async with harness.db() as session:
        assert await expire_going_attendances(session, harness.now) == 0
    async with harness.db() as session:
        after = {
            row.id: row.updated_at
            for row in await session.execute(
                select(models.EventAttendance.id, models.EventAttendance.updated_at)
            )
        }
    assert before == after
