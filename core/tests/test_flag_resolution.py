from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import AuditAction, ErrorCode, RestrictionKind
from hmm_contracts.notifications import AlertKind, NotificationType
from hmm_core import models
from hmm_core.feed.confirmations import refresh_member_confirmations
from hmm_core.notifications.service import raise_alert
from hmm_core.platform.errors import ProblemError
from hmm_core.scores.history import load_member_history
from hmm_core.scores.moderation import resolve_flag
from hmm_core.scores.schemas import (
    FlagPenalties,
    ResolveFlagBody,
    RestrictionPenalty,
    XpPenalty,
)
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

EXCLUDE_ONLY = {
    "exclude": True,
    "xp_penalty": None,
    "revoke_confirmations": False,
    "restrictions": [],
    "ban": False,
}


@dataclass(frozen=True, slots=True)
class World:
    admin: models.User
    a: models.User
    b: models.User
    c: models.User
    d: models.User
    attendance: dict[UUID, UUID]


async def world(h: Harness) -> World:
    admin = await h.admin()
    a = await h.user(display_name="Ann")
    b = await h.user(display_name="Zoe")
    c = await h.user(display_name="Amy")
    d = await h.user(display_name="Dan")
    async with h.db() as session:
        event = models.Event(
            slug=str(uuid4()),
            kind="hackathon",
            formation_enabled=False,
            title="Hack",
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
        rows = {
            user.id: models.EventAttendance(
                event_id=event.id,
                user_id=user.id,
                status="attended",
                role="attendee",
                proof_links=[],
                trust="self",
                status_at=h.now,
            )
            for user in (a, b, c, d)
        }
        session.add_all(rows.values())
        await session.flush()
        return World(admin, a, b, c, d, {uid: row.id for uid, row in rows.items()})


async def confirm(h: Harness, w: World, confirmer: models.User, subject: models.User) -> UUID:
    async with h.db() as session:
        row = models.Confirmation(
            fact_type="attendance",
            fact_id=w.attendance[subject.id],
            subject_user_id=subject.id,
            confirmer_id=confirmer.id,
            source="peer",
            ignored=False,
        )
        session.add(row)
        attendance = await session.get(models.EventAttendance, w.attendance[subject.id])
        assert attendance is not None
        attendance.trust = "peer"
        await session.flush()
        return row.id


async def add_flag(
    h: Harness,
    owner: models.User,
    subject_type: str,
    subject_id: UUID,
    related: list[UUID] | None = None,
) -> UUID:
    async with h.db() as session:
        row = models.Flag(
            user_id=owner.id,
            subject_type=subject_type,
            subject_id=subject_id,
            related_user_ids=related or [],
            reason="dup",
            severity="medium",
            status="to_check",
            algo_version="fake-1",
        )
        session.add(row)
        await session.flush()
        return row.id


async def settle(h: Harness, *users: models.User) -> None:
    async with h.db() as session:
        for user in users:
            await refresh_member_confirmations(session, h.jobs, user.id, h.now)


async def resolve(h: Harness, w: World, flag_id: UUID, **body: Any) -> models.Flag:
    async with h.db() as session:
        return await resolve_flag(
            session,
            h.jobs,
            flag_id,
            ResolveFlagBody.model_validate({"note": "checked", **body}),
            w.admin.id,
            h.now,
        )


def url(flag_id: UUID) -> str:
    return f"/api/v1/admin/flags/{flag_id}/resolve"


async def stored_flag(h: Harness, flag_id: UUID) -> models.Flag:
    async with h.db() as session:
        row = await session.get(models.Flag, flag_id)
        assert row is not None
        return row


async def confirmation(h: Harness, confirmation_id: UUID) -> models.Confirmation:
    async with h.db() as session:
        row = await session.get(models.Confirmation, confirmation_id)
        assert row is not None
        return row


async def trust(h: Harness, w: World, user: models.User) -> str:
    async with h.db() as session:
        row = await session.get(models.EventAttendance, w.attendance[user.id])
        assert row is not None
        return row.trust


async def audits(h: Harness, action: AuditAction) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def notifications(h: Harness, user: models.User) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.user_id == user.id)
            )
        )


async def recomputes(h: Harness, user: models.User) -> int:
    rows = await h.pending_jobs("recompute_member")
    return sum(
        1 for row in rows if row["status"] == "todo" and row["args"] == {"user_id": str(user.id)}
    )


async def count(h: Harness, model: type[Any]) -> int:
    async with h.db() as session:
        value = await session.scalar(select(func.count()).select_from(model))
        return int(value or 0)


async def counts(h: Harness) -> tuple[int, int, int, int]:
    return (
        await count(h, models.XpAdjustment),
        await count(h, models.MemberRestriction),
        await count(h, models.Notification),
        await count(h, models.AuditLog),
    )


def payload(row: models.Notification) -> dict[str, Any]:
    assert isinstance(row.payload, dict)
    return row.payload


async def test_pardon_is_quiet(harness: Harness) -> None:
    h = harness
    w = await world(h)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    async with await h.client(w.admin) as c:
        response = await c.post(url(flag_id), json={"decision": "pardon", "note": "fine"})
        assert response.status_code == 200, response.text
        body = response.json()
    assert body["status"] == "pardoned"
    assert body["allowed_actions"] == []
    assert body["resolved_by"] == str(w.admin.id)
    assert body["resolution_note"] == "fine"
    assert body["penalty"] is None
    assert body["subject_content"]["status"] == "attended"
    row = await stored_flag(h, flag_id)
    assert row.status == "pardoned"
    assert row.resolved_by == w.admin.id
    assert row.resolved_at == h.now
    assert row.resolution_note == "fine"
    assert row.penalty is None
    [audit] = await audits(h, AuditAction.FLAG_PARDON)
    assert audit.before == {"status": "to_check"}
    assert audit.after == {"status": "pardoned"}
    assert audit.reason == "fine"
    assert await notifications(h, w.a) == []
    assert await recomputes(h, w.a) == 1


async def test_pardon_restores_confirmation_and_resolves_alert(harness: Harness) -> None:
    h = harness
    w = await world(h)
    confirmation_id = await confirm(h, w, w.b, w.a)
    flag_id = await add_flag(h, w.a, "confirmation", confirmation_id)
    await settle(h, w.b)
    assert (await confirmation(h, confirmation_id)).ignored
    assert await trust(h, w, w.a) == "self"
    row = await stored_flag(h, flag_id)
    key = f"suspicious_activity:{w.a.id}:{row.created_at.date().isoformat()}"
    async with h.db() as session:
        for user, dedupe_key in (
            (w.a, key),
            (w.c, f"suspicious_activity:{w.c.id}:{h.now.date().isoformat()}"),
        ):
            await raise_alert(
                session,
                AlertKind.SUSPICIOUS_ACTIVITY,
                h.now,
                title="Suspicious",
                message="Flags",
                link=f"/admin/flags?user={user.id}",
                subject_type="user",
                subject_id=user.id,
                dedupe_key=dedupe_key,
            )
    await resolve(h, w, flag_id, decision="pardon")
    stored = await confirmation(h, confirmation_id)
    assert stored.ignored is False
    assert await trust(h, w, w.a) == "peer"
    assert await recomputes(h, w.a) == 1
    assert await recomputes(h, w.b) == 0
    async with h.db() as session:
        alerts = {
            alert.subject_id: alert for alert in await session.scalars(select(models.AdminAlert))
        }
    assert alerts[w.a.id].status == "resolved"
    assert alerts[w.a.id].resolution_note == "checked"
    assert alerts[w.a.id].resolved_by == w.admin.id
    assert alerts[w.c.id].status == "open"


async def test_punish_exclude_only(harness: Harness) -> None:
    h = harness
    w = await world(h)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    row = await resolve(h, w, flag_id, decision="punish", penalties={})
    assert row.status == "punished"
    assert row.penalty == EXCLUDE_ONLY
    [audit] = await audits(h, AuditAction.FLAG_PUNISH)
    assert audit.after == {"status": "punished", "penalty": EXCLUDE_ONLY}
    assert audit.reason == "checked"
    [notification] = await notifications(h, w.a)
    assert notification.type == NotificationType.PENALTY_APPLIED
    data = payload(notification)
    assert data["xp_delta"] == 0
    assert data["reason"] == "checked"
    assert data.get("restriction_id") is None
    assert data.get("restriction_kind") is None
    assert data.get("expires_at") is None
    assert notification.dedupe_key == f"penalty_applied:{flag_id}"
    assert await recomputes(h, w.a) == 1
    async with h.db() as session:
        history = await load_member_history(session, w.a.id, h.now)
    [fact] = [fact for fact in history.facts if fact.id == w.attendance[w.a.id]]
    assert fact.excluded is True


async def test_punish_xp(harness: Harness) -> None:
    h = harness
    w = await world(h)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    async with await h.client(w.a) as c:
        before = (await c.get(f"/api/v1/users/{w.a.id}/metrics")).json()["xp"]
    await resolve(
        h, w, flag_id, decision="punish", penalties={"xp_penalty": {"amount": 4.5}}, reason="farm"
    )
    async with h.db() as session:
        [adjustment] = list(await session.scalars(select(models.XpAdjustment)))
    assert float(adjustment.amount) == -4.5
    assert adjustment.flag_id == flag_id
    assert adjustment.admin_id == w.admin.id
    assert adjustment.reason == "farm"
    [audit] = await audits(h, AuditAction.XP_ADJUST)
    assert audit.entity_id == str(adjustment.id)
    assert audit.after == {
        "user_id": str(w.a.id),
        "amount": -4.5,
        "reason": "farm",
        "flag_id": str(flag_id),
    }
    [notification] = await notifications(h, w.a)
    assert payload(notification)["xp_delta"] == -4
    async with await h.client(w.a) as c:
        after = (await c.get(f"/api/v1/users/{w.a.id}/metrics")).json()["xp"]
    assert before - after == pytest.approx(4.5)


async def test_punish_revoke(harness: Harness) -> None:
    h = harness
    w = await world(h)
    first = await confirm(h, w, w.a, w.b)
    second = await confirm(h, w, w.a, w.c)
    outside = await confirm(h, w, w.d, w.a)
    flag_id = await add_flag(h, w.a, "ring", uuid4(), [w.b.id, w.c.id])
    await resolve(h, w, flag_id, decision="punish", penalties={"revoke_confirmations": True})
    assert (await confirmation(h, first)).withdrawn_at == h.now
    assert (await confirmation(h, second)).withdrawn_at == h.now
    untouched = await confirmation(h, outside)
    assert untouched.withdrawn_at is None and untouched.ignored is False
    [audit] = await audits(h, AuditAction.CONFIRMATIONS_REVOKE)
    assert audit.after == {"confirmation_ids": sorted([str(first), str(second)]), "count": 2}
    assert await trust(h, w, w.b) == "self"
    assert await trust(h, w, w.c) == "self"
    assert await trust(h, w, w.a) == "peer"
    for user in (w.a, w.b, w.c):
        assert await recomputes(h, user) == 1


async def test_punish_revoke_scoped_to_confirmation(harness: Harness) -> None:
    h = harness
    w = await world(h)
    own = await confirm(h, w, w.a, w.b)
    other = await confirm(h, w, w.c, w.b)
    flag_id = await add_flag(h, w.a, "confirmation", own)
    await resolve(h, w, flag_id, decision="punish", penalties={"revoke_confirmations": True})
    assert (await confirmation(h, own)).withdrawn_at == h.now
    assert (await confirmation(h, other)).withdrawn_at is None
    assert await trust(h, w, w.b) == "peer"


async def test_punish_restrictions(harness: Harness) -> None:
    h = harness
    w = await world(h)
    own = await confirm(h, w, w.a, w.b)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    until = h.now + timedelta(days=3)
    await resolve(
        h,
        w,
        flag_id,
        decision="punish",
        penalties={
            "restrictions": [
                {"kind": "confirming", "until": until.isoformat()},
                {"kind": "mentoring", "until": None},
            ]
        },
    )
    async with h.db() as session:
        rows = list(
            await session.scalars(
                select(models.MemberRestriction).order_by(models.MemberRestriction.id)
            )
        )
    assert [(row.kind, row.until, row.flag_id) for row in rows] == [
        ("confirming", until, flag_id),
        ("mentoring", None, flag_id),
    ]
    assert len(await audits(h, AuditAction.RESTRICTION_APPLY)) == 2
    [notification] = await notifications(h, w.a)
    data = payload(notification)
    assert data["restriction_id"] == str(rows[0].id)
    assert data["restriction_kind"] == RestrictionKind.CONFIRMING
    assert datetime.fromisoformat(data["expires_at"]) == until
    assert (await confirmation(h, own)).ignored is True
    assert await trust(h, w, w.b) == "self"
    second = await add_flag(h, w.b, "attendance", w.attendance[w.b.id])
    snapshot = await counts(h)
    async with await h.client(w.admin) as c:
        response = await c.post(
            url(second),
            json={
                "decision": "punish",
                "note": "late",
                "penalties": {
                    "restrictions": [
                        {"kind": "mentoring", "until": (h.now - timedelta(days=1)).isoformat()}
                    ]
                },
            },
        )
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    assert await counts(h) == snapshot
    assert (await stored_flag(h, second)).status == "to_check"


async def test_punish_ban(harness: Harness) -> None:
    h = harness
    w = await world(h)
    async with await h.client(w.a):
        pass
    async with h.db() as session:
        assert await session.scalar(
            select(func.count()).where(models.UserSession.user_id == w.a.id)
        )
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    await resolve(h, w, flag_id, decision="punish", penalties={"ban": True}, reason="fraud")
    async with h.db() as session:
        user = await session.get(models.User, w.a.id)
        assert user is not None and user.banned_at == h.now
        assert not await session.scalar(
            select(func.count()).where(models.UserSession.user_id == w.a.id)
        )
    [audit] = await audits(h, AuditAction.USER_BAN)
    assert audit.reason == "fraud"
    [notification] = await notifications(h, w.a)
    assert notification.type == NotificationType.PENALTY_APPLIED
    other_admin = await h.admin()
    admin_flag = await add_flag(h, other_admin, "profile", other_admin.id)
    async with await h.client(w.admin) as c:
        response = await c.post(
            url(admin_flag),
            json={"decision": "punish", "note": "x", "penalties": {"ban": True}},
        )
        assert_problem(response, 409, ErrorCode.INVALID_STATE)
    assert (await stored_flag(h, admin_flag)).status == "to_check"


async def test_punish_all_penalties(harness: Harness) -> None:
    h = harness
    w = await world(h)
    own = await confirm(h, w, w.a, w.b)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.b.id])
    row = await resolve(
        h,
        w,
        flag_id,
        decision="punish",
        reason="ring",
        penalties=FlagPenalties(
            xp_penalty=XpPenalty(amount=10),
            revoke_confirmations=True,
            restrictions=[RestrictionPenalty(kind=RestrictionKind.CREATING_EVENTS, until=None)],
            ban=True,
        ).model_dump(mode="json"),
    )
    assert isinstance(row.penalty, dict) and row.penalty["exclude"] is True
    assert (await confirmation(h, own)).withdrawn_at == h.now
    async with h.db() as session:
        [adjustment] = list(await session.scalars(select(models.XpAdjustment)))
        assert float(adjustment.amount) == -10
        assert await session.scalar(select(func.count()).select_from(models.MemberRestriction)) == 1
        user = await session.get(models.User, w.a.id)
        assert user is not None and user.banned_at == h.now
    [notification] = await notifications(h, w.a)
    assert payload(notification)["xp_delta"] == -10
    for action in (
        AuditAction.XP_ADJUST,
        AuditAction.CONFIRMATIONS_REVOKE,
        AuditAction.RESTRICTION_APPLY,
        AuditAction.USER_BAN,
        AuditAction.FLAG_PUNISH,
    ):
        assert len(await audits(h, action)) == 1


async def test_ring_pardon_restores_related(harness: Harness) -> None:
    h = harness
    w = await world(h)
    ids = [
        await confirm(h, w, w.a, w.b),
        await confirm(h, w, w.b, w.c),
        await confirm(h, w, w.c, w.a),
    ]
    flag_id = await add_flag(h, w.a, "ring", uuid4(), [w.b.id, w.c.id])
    await settle(h, w.a, w.b, w.c)
    for confirmation_id in ids:
        assert (await confirmation(h, confirmation_id)).ignored
    async with await h.client(w.admin) as c:
        response = await c.post(url(flag_id), json={"decision": "pardon", "note": "ok"})
        assert response.status_code == 200, response.text
        body = response.json()
    assert [ref["display_name"] for ref in body["related_members"]] == ["Amy", "Zoe"]
    for confirmation_id in ids:
        assert (await confirmation(h, confirmation_id)).ignored is False
    for user in (w.a, w.b, w.c):
        assert await trust(h, w, user) == "peer"
        assert await recomputes(h, user) == 1


async def test_repeated_and_invalid_decisions(harness: Harness) -> None:
    h = harness
    w = await world(h)
    flag_id = await add_flag(h, w.a, "attendance", w.attendance[w.a.id])
    await resolve(
        h,
        w,
        flag_id,
        decision="punish",
        penalties={
            "xp_penalty": {"amount": 2},
            "restrictions": [{"kind": "mentoring", "until": None}],
        },
    )
    snapshot = await counts(h)
    with pytest.raises(ProblemError) as error:
        await resolve(h, w, flag_id, decision="pardon")
    assert error.value.code == ErrorCode.INVALID_STATE
    async with await h.client(w.admin) as c:
        assert_problem(
            await c.post(url(flag_id), json={"decision": "punish", "note": "again"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert_problem(
            await c.post(url(uuid4()), json={"decision": "pardon", "note": "x"}),
            404,
            ErrorCode.NOT_FOUND,
        )
        other = await add_flag(h, w.b, "attendance", w.attendance[w.b.id])
        assert_problem(
            await c.post(
                url(other), json={"decision": "pardon", "note": "x", "penalties": {"ban": True}}
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
    assert await counts(h) == snapshot
    assert (await stored_flag(h, other)).status == "to_check"


async def test_planted_ring_punish(harness: Harness) -> None:
    h = harness
    w = await world(h)
    ids = [await confirm(h, w, w.b, w.c), await confirm(h, w, w.c, w.b)]
    flag_id = await add_flag(h, w.a, "ring", uuid4(), [w.b.id, w.c.id])
    await settle(h, w.b, w.c)
    for confirmation_id in ids:
        assert (await confirmation(h, confirmation_id)).ignored
    row = await resolve(
        h,
        w,
        flag_id,
        decision="punish",
        penalties={"ban": True, "xp_penalty": {"amount": 5}},
    )
    assert isinstance(row.penalty, dict) and row.penalty["exclude"] is True
    async with h.db() as session:
        banned = {
            user.id: user.banned_at
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_([w.a.id, w.b.id, w.c.id]))
            )
        }
    assert banned == {w.a.id: h.now, w.b.id: None, w.c.id: None}
    for confirmation_id in ids:
        assert (await confirmation(h, confirmation_id)).ignored is False
    assert await trust(h, w, w.b) == "peer"
    assert await trust(h, w, w.c) == "peer"
    for user in (w.a, w.b, w.c):
        assert await recomputes(h, user) == 1
