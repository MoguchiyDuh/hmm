from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


@dataclass(frozen=True)
class ModerationSetup:
    project: models.Project
    author: models.User
    target: models.User
    reporter: models.User
    review: models.Review
    second: models.Review
    hidden: models.Review
    removed: models.Review


@pytest.fixture
async def moderation(harness: Harness) -> ModerationSetup:
    author = await harness.user(login="author")
    target = await harness.user(login="target")
    reporter = await harness.user(login="reporter")
    now = harness.now
    async with harness.db() as session:
        project = models.Project(
            name="Aurora",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            origin="standalone",
            lead_user_id=target.id,
        )
        session.add(project)
        await session.flush()
        window = models.ReviewWindow(
            subject_type="project",
            subject_id=project.id,
            opens_at=now - timedelta(days=8),
            closes_at=now - timedelta(days=1),
            revealed_at=now - timedelta(days=1),
            participants={
                str(author.id): "member",
                str(target.id): "lead",
                str(reporter.id): "member",
            },
        )
        session.add(window)
        await session.flush()

        def review(
            reviewer: models.User, reviewee: models.User, rating: int, age: int, **fields: object
        ) -> models.Review:
            return models.Review(
                window_id=window.id,
                subject_type="project",
                subject_id=project.id,
                reviewer_id=reviewer.id,
                reviewee_id=reviewee.id,
                context="lead" if reviewee.id == target.id else "member",
                rating=rating,
                comment=f"Comment {age}",
                created_at=now - timedelta(hours=age),
                **fields,
            )

        main = review(author, target, 3, 1, revealed_at=window.revealed_at)
        second = review(reporter, target, 5, 2, revealed_at=window.revealed_at)
        hidden = review(target, author, 6, 3)
        removed = review(
            target,
            reporter,
            2,
            4,
            revealed_at=window.revealed_at,
            removed_at=now - timedelta(hours=1),
            removed_reason="Spam",
        )
        session.add_all([main, second, hidden, removed])
        await session.flush()
    return ModerationSetup(project, author, target, reporter, main, second, hidden, removed)


async def test_report_is_idempotent_and_raises_one_info_alert(
    harness: Harness, moderation: ModerationSetup
) -> None:
    case = moderation
    path = f"/api/v1/reviews/{case.review.id}/report"
    async with await harness.client(case.reporter) as client:
        response = await client.post(path, json={"reason": "Abusive"})
        assert response.status_code == 201, response.text
        first = response.json()
        assert first["review_id"] == str(case.review.id)
        assert first["reporter_id"] == str(case.reporter.id)
        assert first["reason"] == "Abusive"
        assert first["status"] == "open"
        response = await client.post(path, json={"reason": "Abusive"})
        assert response.status_code == 201, response.text
        assert response.json()["id"] == first["id"]
    async with harness.db() as session:
        report = (await session.scalars(select(models.ReviewReport))).one()
        assert report.status == "open"
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.type == "review_reported"
        assert alert.severity == "info"
        assert alert.dedupe_key == f"review_reported:{case.review.id}"
        assert alert.subject_type == "review"
        assert alert.subject_id == case.review.id
        assert isinstance(alert.payload, dict)
        assert alert.payload["subject_title"] == case.project.name
        assert alert.payload["link"] == f"/projects/{case.project.id}"
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0


@pytest.mark.parametrize(
    ("scenario", "status", "code", "detail"),
    (
        ("hidden", 409, ErrorCode.INVALID_STATE, "Review is not revealed"),
        ("removed", 409, ErrorCode.INVALID_STATE, "Review has been removed"),
        ("own", 403, ErrorCode.FORBIDDEN, "You cannot report your own review"),
        ("missing", 404, ErrorCode.NOT_FOUND, None),
    ),
)
async def test_report_rejections(
    harness: Harness,
    moderation: ModerationSetup,
    scenario: str,
    status: int,
    code: ErrorCode,
    detail: str | None,
) -> None:
    case = moderation
    actor = case.author if scenario == "own" else case.reporter
    review_id = {
        "hidden": case.hidden.id,
        "removed": case.removed.id,
        "own": case.review.id,
        "missing": uuid4(),
    }[scenario]
    async with await harness.client(actor) as client:
        problem = assert_problem(
            await client.post(f"/api/v1/reviews/{review_id}/report", json={"reason": "Abusive"}),
            status,
            code,
        )
        if detail is not None:
            assert problem.detail == detail
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.ReviewReport)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 0


async def test_admin_queue_filters_by_open_reports_and_paginates(
    harness: Harness, moderation: ModerationSetup
) -> None:
    case = moderation
    async with await harness.client(case.reporter) as client:
        response = await client.post(
            f"/api/v1/reviews/{case.review.id}/report", json={"reason": "Abusive"}
        )
        assert response.status_code == 201, response.text
        report_id = response.json()["id"]
    async with await harness.client(case.author) as client:
        assert_problem(await client.get("/api/v1/admin/reviews"), 403, ErrorCode.FORBIDDEN)
    admin = await harness.admin()
    async with await harness.client(admin) as client:
        response = await client.get("/api/v1/admin/reviews", params={"reported": "true"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == [str(case.review.id)]
        assert [report["id"] for report in items[0]["reports"]] == [report_id]
        assert items[0]["reviewer"]["id"] == str(case.author.id)
        assert items[0]["allowed_actions"] == ["remove"]
        response = await client.get("/api/v1/admin/reviews", params={"reported": "false"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == [
            str(case.second.id),
            str(case.hidden.id),
            str(case.removed.id),
        ]
        assert all(item["reports"] == [] for item in items)
        assert items[2]["allowed_actions"] == []
        response = await client.get("/api/v1/admin/reviews")
        assert response.status_code == 200, response.text
        everything = [item["id"] for item in response.json()["items"]]
        assert everything == [
            str(case.review.id),
            str(case.second.id),
            str(case.hidden.id),
            str(case.removed.id),
        ]
        assert response.json()["next_cursor"] is None
        seen: list[str] = []
        cursor: str | None = None
        for index in range(4):
            params = {"limit": "1"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get("/api/v1/admin/reviews", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["items"]) == 1
            seen.append(page["items"][0]["id"])
            cursor = page["next_cursor"]
            assert (cursor is not None) == (index < 3)
        assert seen == everything


async def test_remove_review_resolves_reports_audits_notifies_and_is_idempotent(
    harness: Harness, moderation: ModerationSetup
) -> None:
    case = moderation
    profile = f"/api/v1/users/{case.target.id}/reviews"
    async with await harness.client(case.reporter) as client:
        response = await client.post(
            f"/api/v1/reviews/{case.review.id}/report", json={"reason": "Abusive"}
        )
        assert response.status_code == 201, response.text
        response = await client.get(profile)
        assert response.status_code == 200, response.text
        before = response.json()
        assert str(case.review.id) in {item["id"] for item in before["items"]}
        assert before["reputation"] == [{"context": "lead", "average": 4.0, "count": 2}]
    admin = await harness.admin()
    path = f"/api/v1/admin/reviews/{case.review.id}/remove"
    async with await harness.client(admin) as client:
        response = await client.post(path, json={"reason": "Harassment"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(case.review.id)
        assert body["removed_by"] == str(admin.id)
        assert body["removed_reason"] == "Harassment"
        assert body["allowed_actions"] == []
        assert body["reviewer"]["id"] == str(case.author.id)
        assert [report["status"] for report in body["reports"]] == ["resolved"]
    async with harness.db() as session:
        review = await session.get(models.Review, case.review.id)
        assert review is not None
        assert review.removed_at == harness.now
        assert review.removed_by == admin.id
        assert review.removed_reason == "Harassment"
        report = (await session.scalars(select(models.ReviewReport))).one()
        assert report.status == "resolved"
        audit = (
            await session.scalars(
                select(models.AuditLog).where(models.AuditLog.entity_id == str(case.review.id))
            )
        ).one()
        assert audit.action == "review_remove"
        assert audit.entity_type == "review"
        assert audit.actor_id == admin.id
        assert audit.actor_kind == "admin"
        assert audit.reason == "Harassment"
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.status == "resolved"
        assert alert.resolved_by == admin.id
        notification = (
            await session.scalars(
                select(models.Notification).where(models.Notification.type == "review_removed")
            )
        ).one()
        assert notification.user_id == case.author.id
        assert isinstance(notification.payload, dict)
        assert notification.payload["reason"] == "Harassment"
        assert notification.payload["subject_title"] == case.project.name
        assert notification.payload["review_id"] == str(case.review.id)
        assert notification.payload["reviewee"] == {
            "user_id": str(case.target.id),
            "display_name": case.target.display_name,
        }
    async with await harness.client(case.reporter) as client:
        response = await client.get(profile)
        assert response.status_code == 200, response.text
        after = response.json()
        assert str(case.review.id) not in {item["id"] for item in after["items"]}
        assert after["reputation"] == [{"context": "lead", "average": 5.0, "count": 1}]
    harness.advance(timedelta(minutes=5))
    async with await harness.client(admin) as client:
        response = await client.post(path, json={"reason": "Different"})
        assert response.status_code == 200, response.text
        again = response.json()
        assert again["removed_at"] == body["removed_at"]
        assert again["removed_reason"] == "Harassment"
        assert [report["status"] for report in again["reports"]] == ["resolved"]
        assert_problem(
            await client.post(f"/api/v1/admin/reviews/{uuid4()}/remove", json={"reason": "x"}),
            404,
            ErrorCode.NOT_FOUND,
        )
    async with await harness.client(case.author) as client:
        assert_problem(
            await client.post(path, json={"reason": "Harassment"}), 403, ErrorCode.FORBIDDEN
        )
    async with harness.db() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.AuditLog)
                .where(models.AuditLog.entity_id == str(case.review.id))
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.Notification)
                .where(models.Notification.type == "review_removed")
            )
            == 1
        )
        statuses = list(await session.scalars(select(models.ReviewReport.status)))
        assert statuses == ["resolved"]
        for model in (models.XpAdjustment, models.FactAward, models.MemberMetric):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert await harness.pending_jobs() == []
