from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from harness import Harness, MemoryObjectStore
from hmm_contracts.common import (
    AuditAction,
    AuditEntityType,
    CvTextStatus,
    DeliveryStatus,
    ErrorCode,
    GithubSnapshotStatus,
    MemberReviewStatus,
    NotificationChannel,
    QuizStatus,
    SubmissionStatus,
)
from hmm_contracts.notifications import NotificationType
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

QUEUE = "/api/v1/admin/member-reviews"


async def seed_review(
    h: Harness,
    member: models.User,
    *,
    status: MemberReviewStatus = MemberReviewStatus.PENDING,
    opened_at: datetime | None = None,
    discrepancies: list[str] | None = None,
    admin_note: str | None = None,
) -> models.MemberReview:
    async with h.db() as session:
        review = models.MemberReview(
            user_id=member.id,
            status=status,
            opened_at=opened_at or h.now,
            reviewed_by=None,
            reviewed_at=None,
            admin_note=admin_note,
            discrepancies=discrepancies or [],
        )
        session.add(review)
        await session.flush()
        await session.refresh(review)
        return review


async def seed_submission(
    h: Harness,
    member: models.User,
    admin: models.User,
    *,
    version: int = 1,
    quiz_status: QuizStatus = QuizStatus.ARCHIVED,
    answers: dict[str, Any] | None = None,
) -> models.OnboardingSubmission:
    async with h.db() as session:
        quiz = models.Quiz(
            version=version,
            title="Onboarding",
            status=quiz_status,
            created_by=admin.id,
            published_at=h.now,
        )
        session.add(quiz)
        await session.flush()
        submission = models.OnboardingSubmission(
            user_id=member.id,
            quiz_id=quiz.id,
            answers=answers or {},
            status=SubmissionStatus.SUBMITTED,
            submitted_at=h.now,
        )
        session.add(submission)
        await session.flush()
        await session.refresh(submission)
        return submission


async def count(h: Harness, model: type[models.Base], *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def load_review(h: Harness, member: models.User) -> models.MemberReview:
    async with h.db() as session:
        review = await session.get(models.MemberReview, member.id)
        assert review is not None
        return review


def override_score(area: models.Area, member: models.User, admin: models.User, h: Harness) -> Any:
    return models.AreaScore(
        user_id=member.id,
        area_id=area.id,
        calculated=Decimal("3.00"),
        calc_reason="seed",
        override=Decimal("8.00"),
        override_by=admin.id,
        override_reason="interview result",
        override_evidence="interview",
        override_at=h.now,
    )


async def test_list_filters_and_pagination(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    first, second, third = await h.user(), await h.user(), await h.user()
    await seed_review(h, first, opened_at=h.now - timedelta(hours=3))
    await seed_review(h, second, opened_at=h.now - timedelta(hours=1))
    await seed_review(
        h, third, opened_at=h.now - timedelta(hours=2), status=MemberReviewStatus.REVIEWED
    )
    async with await h.client(admin) as c:
        response = await c.get(QUEUE)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["user"]["id"] for item in items] == [
            str(first.id),
            str(third.id),
            str(second.id),
        ]
        for item, user in zip(items, (first, third, second), strict=True):
            assert item["user"]["display_name"] == user.display_name
            assert item["admin_note"] is None
            assert item["discrepancies"] == []
        assert items[0]["status"] == "pending"
        assert datetime.fromisoformat(items[0]["opened_at"]) == h.now - timedelta(hours=3)
        assert items[0]["reviewed_by"] is None
        assert items[0]["reviewed_at"] is None
        assert items[0]["allowed_actions"] == ["edit", "complete"]
        assert items[2]["allowed_actions"] == ["edit", "complete"]
        assert items[1]["status"] == "reviewed"
        assert items[1]["allowed_actions"] == ["edit"]
        response = await c.get(QUEUE, params={"status": "pending"})
        assert len(response.json()["items"]) == 2
        response = await c.get(QUEUE, params={"status": "reviewed"})
        assert [item["user"]["id"] for item in response.json()["items"]] == [str(third.id)]
        response = await c.get(QUEUE, params={"limit": 2})
        page = response.json()
        assert len(page["items"]) == 2
        assert page["next_cursor"]
        response = await c.get(QUEUE, params={"limit": 2, "cursor": page["next_cursor"]})
        page = response.json()
        assert [item["user"]["id"] for item in page["items"]] == [str(second.id)]
        assert page["next_cursor"] is None
        assert_problem(
            await c.get(QUEUE, params={"status": "bogus"}), 422, ErrorCode.VALIDATION_ERROR
        )
    async with await h.client(first) as c:
        assert_problem(await c.get(QUEUE), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as c:
        assert_problem(await c.get(QUEUE), 401, ErrorCode.UNAUTHENTICATED)


async def test_bundle(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    member = await h.user()
    h.inject_storage(MemoryObjectStore())
    first = await seed_submission(h, member, admin, version=1, answers={"years": {"value": 1}})
    h.advance(timedelta(minutes=1))
    second = await seed_submission(h, member, admin, version=2, answers={"years": {"value": 2}})
    async with h.db() as session:
        session.add_all(
            [
                models.CvFile(
                    user_id=member.id,
                    object_key=f"cv/{member.id}",
                    filename="cv.md",
                    mime="text/markdown",
                    size_bytes=4,
                    uploaded_at=h.now,
                    text="backend",
                    text_status=CvTextStatus.OK,
                    text_error=None,
                ),
                models.GithubSnapshot(
                    user_id=member.id,
                    fetched_at=h.now,
                    status=GithubSnapshotStatus.OK,
                    error=None,
                    profile={"login": "octo", "account_created_at": "2020-01-01T00:00:00Z"},
                    repos=[],
                    languages={"python": 1000},
                    stats={
                        "followers": 3,
                        "stars_total": 4,
                        "public_repos": 5,
                        "contributions_last_year": 6,
                    },
                ),
                override_score(areas["backend"], member, admin, h),
                models.AreaScore(
                    user_id=member.id,
                    area_id=areas["frontend"].id,
                    calculated=Decimal("4.00"),
                    calc_reason="seed",
                ),
            ]
        )
    hint = "High quiz score in backend without CV or GitHub evidence"
    await seed_review(h, member, discrepancies=[hint])
    async with await h.client(admin) as c:
        response = await c.get(f"{QUEUE}/{member.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["submissions"]] == [str(first.id), str(second.id)]
    assert [item["quiz"]["version"] for item in body["submissions"]] == [1, 2]
    assert body["submissions"][0]["answers"] != body["submissions"][1]["answers"]
    assert body["cv"]["filename"] == "cv.md"
    assert "expires=300" in body["cv"]["url"]
    assert body["cv"]["text"] == "backend"
    assert body["cv"]["text_status"] == "ok"
    assert body["cv"]["text_error"] is None
    assert body["github"]["login"] == "octo"
    assert body["github"]["followers"] == 3
    assert body["github"]["languages"] == {"python": 100.0}
    scores = {item["area_key"]: item for item in body["area_scores"]}
    assert scores["backend"]["calculated"] == 3.0
    assert scores["backend"]["override"] == 8.0
    assert scores["backend"]["allowed_actions"] == ["override", "clear_override"]
    assert scores["frontend"]["calculated"] == 4.0
    assert body["discrepancies"] == [hint]
    assert body["allowed_actions"] == ["edit", "complete"]


async def test_bundle_empty(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    await seed_review(h, member)
    async with await h.client(admin) as c:
        response = await c.get(f"{QUEUE}/{member.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cv"] is None
    assert body["github"] is None
    assert body["submissions"] == []
    assert body["area_scores"] == []
    assert body["discrepancies"] == []


async def test_unknown_review(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    async with await h.client(admin) as c:
        for user_id in (member.id, uuid4()):
            url = f"{QUEUE}/{user_id}"
            assert_problem(await c.get(url), 404, ErrorCode.NOT_FOUND)
            assert_problem(await c.patch(url, json={"note": "x"}), 404, ErrorCode.NOT_FOUND)
            assert_problem(await c.post(f"{url}/complete"), 404, ErrorCode.NOT_FOUND)
    assert await count(h, models.AuditLog) == 0
    assert await count(h, models.Notification) == 0
    assert await count(h, models.MemberReview) == 0


async def test_note_edit(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    await seed_review(h, member)
    url = f"{QUEUE}/{member.id}"
    edit = models.AuditLog.action == AuditAction.MEMBER_REVIEW_EDIT
    async with await h.client(admin) as c:
        response = await c.patch(url, json={"note": "Looks legit", "reason": "triage"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["admin_note"] == "Looks legit"
        assert body["status"] == "pending"
        assert body["reviewed_at"] is None
        async with h.db() as session:
            audit = (await session.scalars(select(models.AuditLog).where(edit))).one()
            assert audit.entity_type == AuditEntityType.MEMBER_REVIEW
            assert audit.entity_id == str(member.id)
            assert audit.before == {"admin_note": None}
            assert audit.after == {"admin_note": "Looks legit"}
            assert audit.reason == "triage"
        response = await c.patch(url, json={"note": "Looks legit", "reason": "triage"})
        assert response.status_code == 200, response.text
        assert await count(h, models.AuditLog, edit) == 1
        assert_problem(await c.patch(url, json={"note": "   "}), 422, ErrorCode.VALIDATION_ERROR)
        assert_problem(await c.patch(url, json={}), 422, ErrorCode.VALIDATION_ERROR)
        assert await count(h, models.AuditLog, edit) == 1
        async with h.db() as session:
            review = await session.get(models.MemberReview, member.id)
            assert review is not None
            review.status = MemberReviewStatus.REVIEWED
        response = await c.patch(url, json={"note": "Still legit"})
        assert response.status_code == 200, response.text
        assert response.json()["admin_note"] == "Still legit"
        assert response.json()["status"] == "reviewed"
    assert (await load_review(h, member)).admin_note == "Still legit"
    async with await h.client(member) as c:
        assert_problem(await c.patch(url, json={"note": "mine"}), 403, ErrorCode.FORBIDDEN)


async def test_complete_and_notification(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    member = await h.user()
    await h.link_telegram(member)
    await seed_review(h, member)
    async with h.db() as session:
        session.add(override_score(areas["backend"], member, admin, h))
    jobs = len(await h.pending_jobs())
    async with await h.client(admin) as c:
        response = await c.post(
            f"{QUEUE}/{member.id}/complete",
            json={"note": "Verified at interview", "reason": "done"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "reviewed"
    assert body["reviewed_by"] == str(admin.id)
    assert datetime.fromisoformat(body["reviewed_at"]) == h.now
    assert body["admin_note"] == "Verified at interview"
    assert body["allowed_actions"] == ["edit"]
    review = await load_review(h, member)
    assert review.status == MemberReviewStatus.REVIEWED
    assert review.reviewed_by == admin.id
    assert review.reviewed_at == h.now
    assert review.admin_note == "Verified at interview"
    async with h.db() as session:
        audit = (
            await session.scalars(
                select(models.AuditLog).where(
                    models.AuditLog.action == AuditAction.MEMBER_REVIEW_COMPLETE
                )
            )
        ).one()
        assert audit.entity_type == AuditEntityType.MEMBER_REVIEW
        assert audit.entity_id == str(member.id)
        assert audit.before == {
            "status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "admin_note": None,
        }
        assert audit.after == {
            "status": "reviewed",
            "reviewed_by": str(admin.id),
            "reviewed_at": h.now.isoformat(),
            "admin_note": "Verified at interview",
        }
        assert audit.reason == "done"
        notification = (
            await session.scalars(
                select(models.Notification).where(models.Notification.user_id == member.id)
            )
        ).one()
        assert notification.type == NotificationType.SCORES_REVIEWED
        assert isinstance(notification.payload, dict)
        assert notification.payload["reviewer"] == {
            "user_id": str(admin.id),
            "display_name": admin.display_name,
        }
        assert notification.payload["changes"] == [
            {
                "area_key": "backend",
                "area_name": "Backend",
                "previous_score": 3.0,
                "score": 8.0,
                "reason": "interview result",
            }
        ]
        assert notification.payload["link"] == f"/users/{member.id}"
        delivery = (
            await session.scalars(
                select(models.NotificationDelivery).where(
                    models.NotificationDelivery.notification_id == notification.id
                )
            )
        ).one()
        assert delivery.channel == NotificationChannel.TELEGRAM
        assert delivery.status == DeliveryStatus.PENDING
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 0
    assert len(await h.pending_jobs()) == jobs


async def test_complete_idempotent(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    await seed_review(h, member)
    url = f"{QUEUE}/{member.id}/complete"
    async with await h.client(admin) as c:
        response = await c.post(url, json={"note": "ok"})
        assert response.status_code == 200, response.text
        first = await load_review(h, member)
        h.advance(timedelta(hours=1))
        response = await c.post(url)
        assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["reviewed_at"]) == first.reviewed_at
    assert response.json()["admin_note"] == "ok"
    second = await load_review(h, member)
    assert second.reviewed_at == first.reviewed_at
    assert second.admin_note == first.admin_note
    complete = models.AuditLog.action == AuditAction.MEMBER_REVIEW_COMPLETE
    assert await count(h, models.AuditLog, complete) == 1
    reviewed = models.Notification.type == NotificationType.SCORES_REVIEWED
    assert await count(h, models.Notification, reviewed) == 1


async def test_complete_without_override(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    await seed_review(h, member)
    async with await h.client(admin) as c:
        response = await c.post(f"{QUEUE}/{member.id}/complete")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reviewed"
    assert datetime.fromisoformat(response.json()["reviewed_at"]) == h.now
    async with h.db() as session:
        notification = (
            await session.scalars(
                select(models.Notification).where(
                    models.Notification.type == NotificationType.SCORES_REVIEWED
                )
            )
        ).one()
        assert isinstance(notification.payload, dict)
        assert notification.payload["changes"] == []
        reviewer = notification.payload["reviewer"]
        assert isinstance(reviewer, dict)
        assert reviewer["user_id"] == str(admin.id)
    assert (await load_review(h, member)).reviewed_at == h.now


async def test_review_reopens_after_resubmission(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.db() as session:
        session.add(
            models.Quiz(
                version=1,
                title="Onboarding",
                status=QuizStatus.PUBLISHED,
                created_by=admin.id,
                published_at=h.now,
            )
        )
    member = await h.user(onboarded=False)
    complete = f"{QUEUE}/{member.id}/complete"
    async with await h.client(member) as m, await h.client(admin) as a:
        response = await m.post("/api/v1/onboarding/submit")
        assert response.status_code == 200, response.text
        response = await a.post(complete, json={"note": "first pass"})
        assert response.status_code == 200, response.text
        response = await a.post(f"/api/v1/admin/users/{member.id}/onboarding/reopen")
        assert response.status_code == 200, response.text
        h.advance(timedelta(minutes=5))
        response = await m.post("/api/v1/onboarding/submit")
        assert response.status_code == 200, response.text
        review = await load_review(h, member)
        assert review.status == MemberReviewStatus.PENDING
        assert review.opened_at == h.now
        assert review.reviewed_by is None
        assert review.reviewed_at is None
        assert review.admin_note == "first pass"
        response = await a.post(complete)
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["reviewed_at"]) == h.now
        response = await a.get(QUEUE, params={"status": "reviewed"})
        assert [item["user"]["id"] for item in response.json()["items"]] == [str(member.id)]
    action = models.AuditLog.action == AuditAction.MEMBER_REVIEW_COMPLETE
    assert await count(h, models.AuditLog, action) == 2
    reviewed = (
        models.Notification.type == NotificationType.SCORES_REVIEWED,
        models.Notification.user_id == member.id,
    )
    assert await count(h, models.Notification, *reviewed) == 2


async def test_member_access_never_waits(harness: Harness) -> None:
    h = harness
    await h.seed_areas(("backend", "frontend"))
    member = await h.user()
    await seed_review(h, member)
    before = await load_review(h, member)
    notifications = await count(h, models.Notification)
    audits = await count(h, models.AuditLog)
    url = f"{QUEUE}/{member.id}"
    async with await h.client(member) as c:
        assert (await c.get("/api/v1/areas")).status_code == 200
        assert (await c.get(f"/api/v1/users/{member.id}/scores")).status_code == 200
        assert_problem(await c.get(url), 403, ErrorCode.FORBIDDEN)
        assert_problem(await c.patch(url, json={"note": "x"}), 403, ErrorCode.FORBIDDEN)
        assert_problem(await c.post(f"{url}/complete"), 403, ErrorCode.FORBIDDEN)
    after = await load_review(h, member)
    assert (after.status, after.reviewed_at, after.admin_note, after.opened_at) == (
        before.status,
        before.reviewed_at,
        before.admin_note,
        before.opened_at,
    )
    assert await count(h, models.Notification) == notifications
    assert await count(h, models.AuditLog) == audits
    assert await count(h, models.AdminAlert) == 0
