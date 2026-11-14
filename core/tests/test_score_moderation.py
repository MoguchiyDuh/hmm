from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import AuditAction, AuditEntityType, ErrorCode
from hmm_contracts.notifications import NotificationType
from hmm_core import models
from sqlalchemy import select
from test_api_skeleton import assert_problem

FLAGS = "/api/v1/admin/flags"


def xp_url(user_id: UUID) -> str:
    return f"/api/v1/admin/users/{user_id}/xp-adjustments"


def flag(
    h: Harness,
    user_id: UUID,
    subject_type: str,
    subject_id: UUID,
    *,
    status: str = "to_check",
    related: list[UUID] | None = None,
    minutes: int = 0,
) -> models.Flag:
    return models.Flag(
        user_id=user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        related_user_ids=related or [],
        reason="Suspicious",
        severity="medium",
        status=status,
        algo_version="fake-1",
        created_at=h.now - timedelta(minutes=minutes),
    )


async def notifications(h: Harness, user_id: UUID) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification).where(models.Notification.user_id == user_id)
            )
        )


async def xp_audits(h: Harness) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.action == AuditAction.XP_ADJUST)
            )
        )


async def test_negative_adjustment(harness: Harness) -> None:
    h = harness
    admin, member = await h.admin(), await h.user()
    async with await h.client(admin) as c:
        response = await c.post(xp_url(member.id), json={"amount": -5, "reason": "duplicate fact"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["amount"] == -5.0
        assert body["flag_id"] is None
        assert body["admin_id"] == str(admin.id)
        assert body["user_id"] == str(member.id)
        assert_problem(
            await c.post(xp_url(uuid4()), json={"amount": -5, "reason": "x"}),
            404,
            ErrorCode.NOT_FOUND,
        )
        assert_problem(
            await c.post(xp_url(member.id), json={"reason": "x"}), 422, ErrorCode.VALIDATION_ERROR
        )
    async with await h.client(member) as c:
        assert_problem(
            await c.post(xp_url(member.id), json={"amount": -5, "reason": "x"}),
            403,
            ErrorCode.FORBIDDEN,
        )
    [audit] = await xp_audits(h)
    assert audit.entity_type == AuditEntityType.XP_ADJUSTMENT
    assert audit.entity_id == body["id"]
    assert audit.actor_id == admin.id
    assert audit.after == {"user_id": str(member.id), "amount": -5.0, "reason": "duplicate fact"}
    assert audit.reason == "duplicate fact"
    [notification] = await notifications(h, member.id)
    assert notification.type == NotificationType.PENALTY_APPLIED
    assert isinstance(notification.payload, dict)
    assert notification.payload["xp_delta"] == -5
    assert notification.payload["link"] == f"/users/{member.id}"
    assert await h.pending_jobs("recompute_member") == []


async def test_positive_adjustment_is_quiet(harness: Harness) -> None:
    h = harness
    admin, member = await h.admin(), await h.user()
    async with await h.client(admin) as c:
        response = await c.post(xp_url(member.id), json={"amount": 3, "reason": "correction"})
        assert response.status_code == 201, response.text
    async with h.db() as session:
        [row] = list(
            await session.scalars(
                select(models.XpAdjustment).where(models.XpAdjustment.user_id == member.id)
            )
        )
        assert row.amount == Decimal("3.0")
    assert len(await xp_audits(h)) == 1
    assert await notifications(h, member.id) == []


async def test_adjustments_added_by_readers(harness: Harness) -> None:
    h = harness
    admin, member = await h.admin(), await h.user()
    async with h.db() as session:
        session.add(
            models.MemberMetric(
                user_id=member.id,
                xp=Decimal(10),
                activity=Decimal(0),
                algo_version="fake-1",
                input_hash="x" * 64,
                computed_at=h.now,
            )
        )
    async with await h.client(admin) as c:
        response = await c.post(xp_url(member.id), json={"amount": -25, "reason": "penalty"})
        assert response.status_code == 201, response.text
    async with await h.client(member) as c:
        response = await c.get(f"/api/v1/users/{member.id}/metrics")
        assert response.status_code == 200, response.text
        assert response.json()["xp"] == -15.0
    async with h.db() as session:
        metric = await session.get(models.MemberMetric, member.id)
        assert metric is not None and metric.xp == Decimal(10)


async def test_flag_list_filters(harness: Harness) -> None:
    h = harness
    admin, a, b = await h.admin(), await h.user(), await h.user()
    async with h.db() as session:
        rows = [
            flag(h, a.id, "profile", a.id, minutes=3),
            flag(h, a.id, "profile", a.id, status="pardoned", minutes=2),
            flag(h, b.id, "review", uuid4(), status="punished", minutes=1),
            flag(h, b.id, "profile", b.id, minutes=0),
        ]
        session.add_all(rows)
        await session.flush()
        live_a, pardoned, punished, live_b = (str(row.id) for row in rows)
    async with await h.client(admin) as c:
        response = await c.get(FLAGS)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == [live_b, live_a]
        assert all(item["allowed_actions"] == ["resolve"] for item in items)
        items = (await c.get(FLAGS, params={"status": "pardoned"})).json()["items"]
        assert [item["id"] for item in items] == [pardoned]
        assert items[0]["allowed_actions"] == []
        items = (await c.get(FLAGS, params={"status": "punished"})).json()["items"]
        assert [item["id"] for item in items] == [punished]
        items = (await c.get(FLAGS, params={"user": str(a.id)})).json()["items"]
        assert [item["id"] for item in items] == [live_a]
        params = {"status": "punished", "subject_type": "profile"}
        assert (await c.get(FLAGS, params=params)).json()["items"] == []
        items = (await c.get(FLAGS, params={"subject_type": "profile"})).json()["items"]
        assert [item["id"] for item in items] == [live_b, live_a]
    async with await h.client(a) as c:
        assert_problem(await c.get(FLAGS), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        assert_problem(await c.get(FLAGS), 401, ErrorCode.UNAUTHENTICATED)


async def test_flag_subject_content(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    admin = await h.admin()
    zed = await h.user(display_name="Zed")
    amy = await h.user(display_name="Amy")
    max_ = await h.user(display_name="Max")
    gone = await h.user(display_name="Gone")
    area = next(iter(areas.values()))
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
        project = models.Project(
            name="Proj",
            description_md="Description",
            goal="Goal",
            links=[],
            visibility="public",
            status="idea",
            origin="standalone",
        )
        request = models.HelpRequest(
            learner_id=amy.id,
            area_id=area.id,
            topic="Topic",
            goal="Goal",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=7),
        )
        session.add_all([event, project, request])
        await session.flush()
        attendance = models.EventAttendance(
            event_id=event.id,
            user_id=zed.id,
            status="attended",
            role="attendee",
            proof_links=[],
            trust="self",
            status_at=h.now,
        )
        member = models.ProjectMember(
            project_id=project.id,
            user_id=zed.id,
            role="lead",
            state="confirmed",
            trust="self",
            joined_via="create",
            is_team_row=False,
            joined_at=h.now,
        )
        mentorship = models.Mentorship(
            request_id=request.id,
            mentor_id=zed.id,
            learner_id=amy.id,
            area_id=area.id,
            kind="mentoring",
            mentor_score_at_start=Decimal(5),
            learner_score_at_start=Decimal(5),
            status="active",
            started_at=h.now,
            mentor_trust="self",
            learner_trust="self",
        )
        entry = models.ActivityEntry(
            user_id=zed.id,
            type="talk",
            title="Talk",
            description_md="",
            links=[],
            occurred_on=date(2026, 1, 2),
        )
        review = models.Review(
            subject_type="project",
            subject_id=project.id,
            reviewer_id=amy.id,
            reviewee_id=zed.id,
            context="member",
            rating=7,
            comment="ok",
        )
        session.add_all([attendance, member, mentorship, entry, review])
        await session.flush()
        confirmation = models.Confirmation(
            fact_type="attendance",
            fact_id=attendance.id,
            subject_user_id=zed.id,
            confirmer_id=amy.id,
            source="peer",
            ignored=False,
        )
        session.add(confirmation)
        await session.flush()
        ring_ids = [zed.id, amy.id, max_.id, gone.id]
        specs = [
            ("attendance", attendance.id, []),
            ("project_member", member.id, []),
            ("mentorship", mentorship.id, []),
            ("confirmation", confirmation.id, []),
            ("activity_entry", entry.id, []),
            ("review", review.id, []),
            ("profile", zed.id, []),
            ("ring", zed.id, ring_ids),
            ("attendance", uuid4(), []),
        ]
        flags = [
            flag(h, zed.id, subject_type, subject_id, related=related, minutes=index)
            for index, (subject_type, subject_id, related) in enumerate(specs)
        ]
        session.add_all(flags)
        await session.flush()
        ids = [str(row.id) for row in flags]
    async with h.db() as session:
        stored = await session.get(models.User, gone.id)
        assert stored is not None
        await session.delete(stored)
    async with await h.client(admin) as c:
        response = await c.get(FLAGS, params={"limit": 50})
        assert response.status_code == 200, response.text
        items = {item["id"]: item for item in response.json()["items"]}
    content = [items[flag_id]["subject_content"] for flag_id in ids]
    assert content[0] == {
        "event_id": str(event.id),
        "event_title": "Hack",
        "event_slug": event.slug,
        "role": "attendee",
        "status": "attended",
    }
    assert content[1] == {
        "project_id": str(project.id),
        "project_name": "Proj",
        "role": "lead",
        "state": "confirmed",
    }
    assert content[2] == {
        "mentorship_id": str(mentorship.id),
        "mentor_id": str(zed.id),
        "learner_id": str(amy.id),
        "area_key": area.key,
        "kind": "mentoring",
        "status": "active",
    }
    assert content[3] == {
        "confirmation_id": str(confirmation.id),
        "fact_type": "attendance",
        "fact_id": str(attendance.id),
        "confirmer_id": str(amy.id),
        "source": "peer",
        "subject_user_id": str(zed.id),
    }
    assert content[4] == {
        "entry_id": str(entry.id),
        "type": "talk",
        "title": "Talk",
        "occurred_on": "2026-01-02",
    }
    assert content[5] == {
        "review_id": str(review.id),
        "context": "member",
        "rating": 7,
        "reviewer_id": str(amy.id),
        "reviewee_id": str(zed.id),
        "subject_type": "project",
        "subject_id": str(project.id),
    }
    assert content[6] == {"user_id": str(zed.id), "display_name": "Zed", "banned": False}
    assert content[7] == {"member_ids": [str(uid) for uid in sorted(ring_ids)]}
    assert content[8] == {}
    ring = items[ids[7]]
    assert [ref["display_name"] for ref in ring["related_members"]] == ["Amy", "Max", "Zed"]
    assert ring["related_user_ids"] == [str(uid) for uid in ring_ids]
    assert ring["penalty"] is None


async def test_flag_pagination(harness: Harness) -> None:
    h = harness
    admin, member = await h.admin(), await h.user()
    async with h.db() as session:
        rows = [flag(h, member.id, "review", uuid4(), minutes=minutes) for minutes in (2, 0, 1)]
        session.add_all(rows)
        await session.flush()
        expected = [str(rows[1].id), str(rows[2].id), str(rows[0].id)]
    async with await h.client(admin) as c:
        first = (await c.get(FLAGS, params={"limit": 1})).json()
        assert [item["id"] for item in first["items"]] == expected[:1]
        assert first["next_cursor"] is not None
        rest = (await c.get(FLAGS, params={"limit": 2, "cursor": first["next_cursor"]})).json()
        assert [item["id"] for item in rest["items"]] == expected[1:]
        assert rest["next_cursor"] is None
