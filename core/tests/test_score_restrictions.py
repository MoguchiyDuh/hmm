from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ErrorCode, RestrictionKind
from hmm_contracts.notifications import NotificationType
from hmm_core import models
from hmm_core.platform.errors import ProblemError
from hmm_core.scores.restrictions import (
    active_restrictions,
    apply_restriction,
    lift_restriction,
    list_restrictions,
    require_unrestricted,
    restriction_view,
)
from hmm_core.scores.schemas import RestrictionAction
from sqlalchemy import select
from test_api_skeleton import assert_problem

CONFIRMING = RestrictionKind.CONFIRMING
MENTORING = RestrictionKind.MENTORING


@dataclass(frozen=True, slots=True)
class Seed:
    a: models.User
    b: models.User
    admin: models.User
    attendance_id: UUID
    confirmation_id: UUID


@contextmanager
def problem(code: ErrorCode) -> Generator[None]:
    with pytest.raises(ProblemError) as error:
        yield
    assert error.value.code == code


async def seed(h: Harness) -> Seed:
    a, b, admin = await h.user(), await h.user(), await h.admin()
    async with h.db() as session:
        event = models.Event(
            slug=str(uuid4()),
            kind="hackathon",
            formation_enabled=False,
            title="Ended event",
            starts_at=h.now - timedelta(days=2),
            ends_at=h.now - timedelta(days=1),
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
        attendances = [
            models.EventAttendance(
                event_id=event.id,
                user_id=user.id,
                status="attended",
                role="attendee",
                proof_links=[],
                trust="peer" if user is a else "self",
                status_at=h.now,
            )
            for user in (a, b)
        ]
        session.add_all(attendances)
        await session.flush()
        confirmation = models.Confirmation(
            fact_type="attendance",
            fact_id=attendances[0].id,
            subject_user_id=a.id,
            confirmer_id=b.id,
            source="peer",
            ignored=False,
        )
        session.add(confirmation)
        await session.flush()
        return Seed(a, b, admin, attendances[0].id, confirmation.id)


async def fact_state(h: Harness, s: Seed) -> tuple[bool, str]:
    async with h.db() as session:
        confirmation = await session.get(models.Confirmation, s.confirmation_id)
        attendance = await session.get(models.EventAttendance, s.attendance_id)
        assert confirmation is not None and attendance is not None
        return confirmation.ignored, attendance.trust


async def recompute_jobs(h: Harness, user: models.User) -> list[dict[str, object]]:
    rows = await h.pending_jobs("recompute_member")
    return [
        row for row in rows if row["status"] == "todo" and row["args"] == {"user_id": str(user.id)}
    ]


async def apply(
    h: Harness,
    s: Seed,
    user: models.User,
    kind: RestrictionKind = CONFIRMING,
    until_delta: timedelta | None = None,
) -> models.MemberRestriction:
    async with h.db() as session:
        return await apply_restriction(
            session,
            h.jobs,
            user_id=user.id,
            kind=kind,
            until=None if until_delta is None else h.now + until_delta,
            reason="Spam confirmations",
            flag_id=None,
            actor=s.admin.id,
            now=h.now,
        )


async def lift(h: Harness, s: Seed, restriction_id: UUID, reason: str = "Appeal") -> None:
    async with h.db() as session:
        await lift_restriction(session, h.jobs, restriction_id, s.admin.id, reason, h.now)


async def audits(h: Harness, action: AuditAction) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def test_apply_restriction_ignores_confirmations(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    row = await apply(h, s, s.b)
    async with h.db() as session:
        rows = list(await session.scalars(select(models.MemberRestriction)))
    assert [(r.id, r.until, r.flag_id, r.created_by, r.kind) for r in rows] == [
        (row.id, None, None, s.admin.id, "confirming")
    ]
    [audit] = await audits(h, AuditAction.RESTRICTION_APPLY)
    assert audit.actor_id == s.admin.id
    assert audit.actor_kind == ActorKind.ADMIN
    assert audit.entity_type == AuditEntityType.RESTRICTION
    assert audit.entity_id == str(row.id)
    assert audit.before is None
    assert audit.after == {
        "user_id": str(s.b.id),
        "kind": "confirming",
        "until": None,
        "flag_id": None,
        "reason": "Spam confirmations",
    }
    assert audit.reason == "Spam confirmations"
    assert await fact_state(h, s) == (True, "self")
    assert len(await recompute_jobs(h, s.a)) == 1
    assert await recompute_jobs(h, s.b) == []


async def test_require_unrestricted(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    row = await apply(h, s, s.b, MENTORING)
    async with h.db() as session:
        with problem(ErrorCode.RESTRICTED):
            await require_unrestricted(session, s.b.id, MENTORING, h.now)
        for kind in RestrictionKind:
            if kind is not MENTORING:
                await require_unrestricted(session, s.b.id, kind, h.now)
        await require_unrestricted(session, s.a.id, MENTORING, h.now)
    await lift(h, s, row.id)
    async with h.db() as session:
        await require_unrestricted(session, s.b.id, MENTORING, h.now)
    timed = await apply(h, s, s.b, MENTORING, timedelta(hours=1))
    async with h.db() as session:
        assert await active_restrictions(session, s.b.id, h.now) == {MENTORING}
        assert timed.until is not None
        assert await active_restrictions(session, s.b.id, timed.until) == set()
        await require_unrestricted(session, s.b.id, MENTORING, timed.until)


async def test_overlapping_restrictions_lift_individually(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    first = await apply(h, s, s.b)
    second = await apply(h, s, s.b)
    await lift(h, s, first.id)
    assert await fact_state(h, s) == (True, "self")
    async with h.db() as session:
        with problem(ErrorCode.RESTRICTED):
            await require_unrestricted(session, s.b.id, CONFIRMING, h.now)
    await lift(h, s, second.id)
    assert await fact_state(h, s) == (False, "peer")
    async with h.db() as session:
        await require_unrestricted(session, s.b.id, CONFIRMING, h.now)


async def test_lift_errors(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    with problem(ErrorCode.NOT_FOUND):
        await lift(h, s, uuid4())
    row = await apply(h, s, s.b)
    await lift(h, s, row.id)
    with problem(ErrorCode.INVALID_STATE):
        await lift(h, s, row.id)
    expired = await apply(h, s, s.b, MENTORING, timedelta(hours=1))
    h.advance(timedelta(hours=2))
    with problem(ErrorCode.INVALID_STATE):
        await lift(h, s, expired.id)
    async with h.db() as session:
        stored = await session.get(models.MemberRestriction, expired.id)
        assert stored is not None and stored.lifted_at is None


async def test_lift_effects(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    row = await apply(h, s, s.b)
    await lift(h, s, row.id, "Appeal accepted")
    [audit] = await audits(h, AuditAction.RESTRICTION_LIFT)
    assert audit.actor_id == s.admin.id
    assert audit.entity_type == AuditEntityType.RESTRICTION
    assert audit.entity_id == str(row.id)
    assert audit.before == {"lifted_at": None}
    assert audit.after == {"lifted_at": h.now.isoformat()}
    assert audit.reason == "Appeal accepted"
    async with h.db() as session:
        notifications = list(
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == NotificationType.RESTRICTION_LIFTED
                )
            )
        )
    [notification] = notifications
    assert notification.user_id == s.b.id
    assert isinstance(notification.payload, dict)
    assert notification.payload["restriction_id"] == str(row.id)
    assert notification.payload["restriction_kind"] == "confirming"
    assert notification.payload["link"] == f"/users/{s.b.id}"
    assert notification.payload["reason"] == "Appeal accepted"
    assert len(await recompute_jobs(h, s.a)) == 1
    assert await recompute_jobs(h, s.b) == []


async def test_list_restrictions(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    async with h.db() as session:
        rows = [
            models.MemberRestriction(
                user_id=s.b.id,
                kind=kind,
                until=until,
                reason=label,
                created_by=s.admin.id,
                lifted_at=lifted_at,
                created_at=h.now - timedelta(minutes=minutes),
            )
            for kind, until, lifted_at, label, minutes in (
                ("mentoring", h.now - timedelta(hours=1), None, "expired", 30),
                ("confirming", None, h.now - timedelta(minutes=5), "lifted", 20),
                ("creating_events", h.now + timedelta(days=1), None, "active", 10),
            )
        ]
        session.add(
            models.MemberRestriction(
                user_id=s.a.id, kind="mentoring", until=None, reason="other", created_by=s.admin.id
            )
        )
        session.add_all(rows)
        await session.flush()
        expired, lifted, active = (row.id for row in rows)
        listed = await list_restrictions(session, user_id=s.b.id, active=True, now=h.now)
        assert [row.id for row in listed] == [active]
        assert restriction_view(listed[0], h.now).allowed_actions == [RestrictionAction.LIFT]
        listed = await list_restrictions(session, user_id=s.b.id, active=False, now=h.now)
        assert [row.id for row in listed] == [lifted, expired]
        assert [restriction_view(row, h.now).allowed_actions for row in listed] == [[], []]
        listed = await list_restrictions(session, user_id=s.b.id, active=None, now=h.now)
        assert [row.id for row in listed] == [active, lifted, expired]
        listed = await list_restrictions(session, user_id=None, active=None, now=h.now)
        assert len(listed) == 4
    async with await h.client(s.admin) as c:
        response = await c.get(
            "/api/v1/admin/restrictions", params={"user": str(s.b.id), "active": "true"}
        )
        assert response.status_code == 200, response.text
        [body] = response.json()
        assert body["id"] == str(active)
        assert body["kind"] == "creating_events"
        assert body["allowed_actions"] == ["lift"]
        response = await c.get("/api/v1/admin/restrictions", params={"user": str(s.b.id)})
        assert [item["id"] for item in response.json()] == [
            str(active),
            str(lifted),
            str(expired),
        ]
        response = await c.get("/api/v1/admin/restrictions", params={"active": "false"})
        assert [item["id"] for item in response.json()] == [str(lifted), str(expired)]
    async with await h.client(s.a) as c:
        assert_problem(await c.get("/api/v1/admin/restrictions"), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        assert_problem(await c.get("/api/v1/admin/restrictions"), 401, ErrorCode.UNAUTHENTICATED)


async def test_lift_endpoint(harness: Harness) -> None:
    h = harness
    s = await seed(h)
    row = await apply(h, s, s.b)
    url = f"/api/v1/admin/restrictions/{row.id}"
    async with await h.client(s.admin) as c:
        assert_problem(await c.request("DELETE", url), 422, ErrorCode.VALIDATION_ERROR)
        response = await c.request("DELETE", url, json={"reason": "appeal accepted"})
        assert response.status_code == 204, response.text
        assert_problem(
            await c.request("DELETE", url, json={"reason": "appeal accepted"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await c.request(
                "DELETE", f"/api/v1/admin/restrictions/{uuid4()}", json={"reason": "appeal"}
            ),
            404,
            ErrorCode.NOT_FOUND,
        )
    async with h.db() as session:
        stored = await session.get(models.MemberRestriction, row.id)
        assert stored is not None and stored.lifted_at == h.now
    [audit] = await audits(h, AuditAction.RESTRICTION_LIFT)
    assert audit.reason == "appeal accepted"
    assert audit.actor_id == s.admin.id
