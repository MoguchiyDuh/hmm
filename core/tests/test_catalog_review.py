from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import func, select
from test_api_skeleton import assert_problem
from test_catalog_import import import_item, import_results

ADMIN_EVENTS = "/api/v1/admin/events"


async def count(h: Harness, model: Any, *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def seed_event(
    h: Harness,
    name: str,
    *,
    status: str = "published",
    review_reasons: list[str] | None = None,
    starts_at: datetime | None = None,
    formation_enabled: bool = False,
) -> UUID:
    start = starts_at or h.now + timedelta(days=20)
    async with h.db() as session:
        event = models.Event(
            slug=name,
            kind="hackathon",
            formation_enabled=formation_enabled,
            title=name.title(),
            starts_at=start,
            ends_at=start + timedelta(days=1),
            timezone="UTC",
            format="offline",
            language=[],
            themes=[],
            status=status,
            review_reasons=review_reasons or [],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        return event.id


async def add_source(h: Harness, event_id: UUID, source_id: str) -> None:
    async with h.db() as session:
        session.add(
            models.EventSource(
                event_id=event_id,
                source_id=source_id,
                urls=[f"https://events.example.org/{source_id}"],
                payload={},
                payload_hash="h",
                extractor_version="v",
                first_seen_at=h.now,
                last_seen_at=h.now,
            )
        )


async def add_attendance(
    h: Harness,
    event_id: UUID,
    user: models.User,
    *,
    status: str,
    role: str = "attendee",
    proof_links: list[str] | None = None,
    trust: str = "self",
) -> UUID:
    async with h.db() as session:
        row = models.EventAttendance(
            event_id=event_id,
            user_id=user.id,
            status=status,
            role=role,
            proof_links=proof_links or [],
            trust=trust,
            status_at=h.now - timedelta(days=3),
        )
        session.add(row)
        await session.flush()
        return row.id


async def post(
    h: Harness, user: models.User, path: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    async with await h.client(user) as client:
        if body is None:
            return await client.post(path)
        return await client.post(path, json=body)


async def audits(h: Harness, action: str) -> list[models.AuditLog]:
    async with h.db() as session:
        return list(
            await session.scalars(select(models.AuditLog).where(models.AuditLog.action == action))
        )


async def load(h: Harness, event_id: UUID) -> models.Event:
    async with h.db() as session:
        event = await session.get(models.Event, event_id)
        assert event is not None
        return event


async def test_review_queue(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    draft = await import_results(
        h, import_item(h, confidence=0.5, starts_at=h.now + timedelta(days=30))
    )
    flagged = await seed_event(
        h, "flagged", review_reasons=["possible_duplicate"], starts_at=h.now + timedelta(days=10)
    )
    await seed_event(h, "clean", starts_at=h.now + timedelta(days=5))
    async with await h.client(admin) as client:
        response = await client.get(f"{ADMIN_EVENTS}/review-queue")
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [str(flagged), draft[0]["event_id"]]
    assert [item["review_reasons"] for item in items] == [
        ["possible_duplicate"],
        ["low_confidence"],
    ]
    assert [item["status"] for item in items] == ["published", "draft"]
    assert all("sources" in item and "formation" in item for item in items)


async def test_publish_draft(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    draft = await import_results(h, import_item(h, confidence=0.5))
    event_id = UUID(draft[0]["event_id"])
    assert await count(h, models.EventFormation) == 0
    assert await h.pending_jobs("compute_lead_fits") == []
    response = await post(h, admin, f"{ADMIN_EVENTS}/{event_id}/publish", {"reason": "Looks right"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    assert response.json()["review_reasons"] == []
    async with h.db() as session:
        phase = await session.scalar(
            select(models.EventFormation.phase).where(models.EventFormation.event_id == event_id)
        )
    assert phase == "open"
    assert len(await h.pending_jobs("compute_lead_fits")) == 1
    rows = await audits(h, "event_publish")
    assert len(rows) == 1
    assert isinstance(rows[0].before, dict)
    assert isinstance(rows[0].after, dict)
    assert rows[0].before["status"] == "draft"
    assert rows[0].after["status"] == "published"
    assert rows[0].reason == "Looks right"
    again = await post(h, admin, f"{ADMIN_EVENTS}/{event_id}/publish")
    assert again.status_code == 200, again.text
    assert len(await audits(h, "event_publish")) == 1
    assert len(await h.pending_jobs("compute_lead_fits")) == 1


async def test_keep_published_event(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event_id = await seed_event(h, "kept", review_reasons=["possible_duplicate"])
    response = await post(h, admin, f"{ADMIN_EVENTS}/{event_id}/publish")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    assert response.json()["review_reasons"] == []
    assert (await load(h, event_id)).review_reasons == []
    assert len(await audits(h, "event_publish")) == 1


async def test_review_errors(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    archived = await seed_event(h, "archived", status="archived")
    cancelled = await seed_event(h, "cancelled", status="cancelled")
    rejected = await seed_event(h, "rejected", status="draft", review_reasons=["low_confidence"])
    assert_problem(
        await post(h, admin, f"{ADMIN_EVENTS}/{archived}/publish"), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await post(h, admin, f"{ADMIN_EVENTS}/{cancelled}/publish"), 409, ErrorCode.INVALID_STATE
    )
    first = await post(h, admin, f"{ADMIN_EVENTS}/{rejected}/reject")
    assert first.status_code == 200, first.text
    assert_problem(
        await post(h, admin, f"{ADMIN_EVENTS}/{rejected}/reject"), 409, ErrorCode.INVALID_STATE
    )
    assert_problem(
        await post(h, admin, f"{ADMIN_EVENTS}/{uuid4()}/publish"), 404, ErrorCode.NOT_FOUND
    )
    assert_problem(
        await post(h, member, f"{ADMIN_EVENTS}/{cancelled}/publish"), 403, ErrorCode.FORBIDDEN
    )
    assert_problem(
        await post(h, member, f"{ADMIN_EVENTS}/{cancelled}/reject"), 403, ErrorCode.FORBIDDEN
    )
    assert len(await audits(h, "event_publish")) == 0
    assert len(await audits(h, "event_reject")) == 1


async def test_reject_published_flagged_event(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    event_id = await seed_event(h, "flagged", review_reasons=["possible_duplicate"])
    response = await post(h, admin, f"{ADMIN_EVENTS}/{event_id}/reject", {"reason": "Duplicate"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"
    assert response.json()["review_reasons"] == []
    rows = await audits(h, "event_reject")
    assert len(rows) == 1
    assert rows[0].before == {"status": "published", "review_reasons": ["possible_duplicate"]}
    assert rows[0].after == {"status": "archived", "review_reasons": []}
    assert rows[0].reason == "Duplicate"
    assert await count(h, models.Notification) == 0


async def test_merge_guards(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    member = await h.user()
    source = await seed_event(h, "source", status="draft", review_reasons=["possible_duplicate"])
    target = await seed_event(h, "target")
    await add_source(h, source, "s-source")
    await add_attendance(h, source, member, status="going")

    async def merge(event_id: UUID, into: UUID | str) -> httpx.Response:
        return await post(h, admin, f"{ADMIN_EVENTS}/{event_id}/merge", {"into": str(into)})

    assert_problem(await merge(source, source), 422, ErrorCode.VALIDATION_ERROR)
    assert_problem(await merge(source, uuid4()), 404, ErrorCode.NOT_FOUND)
    async with h.db() as session:
        team = models.Project(
            name="Forming team",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            team_status="forming",
            origin="standalone",
            event_id=source,
        )
        session.add(team)
    assert_problem(await merge(source, target), 409, ErrorCode.INVALID_STATE)
    async with h.db() as session:
        await session.delete(team)
        session.add(
            models.Enrollment(
                event_id=source,
                user_id=member.id,
                status="withdrawn",
                source="self",
                status_at=h.now,
            )
        )
    assert_problem(await merge(source, target), 409, ErrorCode.INVALID_STATE)
    archived_target = await seed_event(h, "archived-target", status="archived")
    archived_source = await seed_event(h, "archived-source", status="archived")
    clean_source = await seed_event(h, "clean-source", status="draft")
    assert_problem(await merge(clean_source, archived_target), 409, ErrorCode.INVALID_STATE)
    assert_problem(await merge(archived_source, target), 409, ErrorCode.INVALID_STATE)
    stored = await load(h, source)
    assert stored.status == "draft"
    assert stored.review_reasons == ["possible_duplicate"]
    assert (await load(h, clean_source)).status == "draft"
    assert await count(h, models.EventSource, models.EventSource.event_id == source) == 1
    assert await count(h, models.EventAttendance, models.EventAttendance.event_id == source) == 1
    assert len(await audits(h, "event_merge")) == 0


async def test_merge_success(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend",))
    admin = await h.admin()
    member_a = await h.user()
    member_b = await h.user()
    member_c = await h.user()
    source = await seed_event(h, "source", status="draft", review_reasons=["possible_duplicate"])
    target = await seed_event(h, "target")
    await add_source(h, source, "s-source")
    await add_source(h, target, "s-target")
    async with h.db() as session:
        session.add(
            models.EventArea(event_id=source, area_id=areas["backend"].id, importance="primary")
        )
        session.add(
            models.EventSourcePerson(
                event_id=source,
                source_id="s-source",
                github_login="someone",
                display_name="Someone",
                role="attendee",
            )
        )
    source_a = await add_attendance(
        h, source, member_a, status="attended", proof_links=["p1", "p1"], trust="trusted"
    )
    source_b = await add_attendance(h, source, member_b, status="going")
    target_a = await add_attendance(
        h, target, member_a, status="going", role="speaker", proof_links=["p2"]
    )
    async with h.db() as session:
        session.add_all(
            [
                models.Confirmation(
                    fact_type="attendance",
                    fact_id=source_a,
                    subject_user_id=member_a.id,
                    confirmer_id=None,
                    source="system",
                    ignored=False,
                ),
                models.Confirmation(
                    fact_type="attendance",
                    fact_id=source_a,
                    subject_user_id=member_a.id,
                    confirmer_id=member_c.id,
                    source="peer",
                    ignored=False,
                ),
            ]
        )
    response = await post(
        h, admin, f"{ADMIN_EVENTS}/{source}/merge", {"into": str(target), "reason": "Same"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(target)
    merged_source = await load(h, source)
    assert merged_source.status == "archived"
    assert merged_source.review_reasons == []
    async with h.db() as session:
        source_ids = sorted(
            await session.scalars(
                select(models.EventSource.source_id).where(models.EventSource.event_id == target)
            )
        )
        attendance_a = await session.scalar(
            select(models.EventAttendance).where(
                models.EventAttendance.user_id == member_a.id,
            )
        )
        attendance_b = await session.get(models.EventAttendance, source_b)
        confirmations = list(
            await session.scalars(
                select(models.Confirmation).where(models.Confirmation.fact_type == "attendance")
            )
        )
        people_events = list(await session.scalars(select(models.EventSourcePerson.event_id)))
    assert source_ids == ["s-source", "s-target"]
    assert attendance_a is not None
    assert attendance_a.id == target_a
    assert attendance_a.event_id == target
    assert attendance_a.status == "attended"
    assert attendance_a.role == "speaker"
    assert attendance_a.proof_links == ["p2", "p1"]
    assert attendance_a.status_at == h.now
    assert attendance_a.trust == "trusted"
    assert (
        await count(h, models.EventAttendance, models.EventAttendance.user_id == member_a.id) == 1
    )
    assert sorted((row.fact_id, row.source) for row in confirmations) == [
        (target_a, "peer"),
        (target_a, "system"),
    ]
    assert attendance_b is not None
    assert attendance_b.event_id == target
    assert people_events == [target]
    rows = await audits(h, "event_merge")
    assert len(rows) == 1
    assert rows[0].entity_id == str(source)
    assert rows[0].after == {"status": "archived", "merged_into": str(target)}
    assert rows[0].reason == "Same"
    assert await count(h, models.Event) == 2
    assert await count(h, models.EventArea, models.EventArea.event_id == source) == 1
    jobs = await h.pending_jobs("recompute_member")
    assert all(job["status"] == "todo" for job in jobs)
    assert sorted(job["args"]["user_id"] for job in jobs) == sorted(
        [str(member_a.id), str(member_b.id)]
    )


async def test_review_full_flow(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    admin = await h.admin()
    member = await h.user()
    first = await import_results(
        h, import_item(h, source_id="s1", title="Py Days 2027", confidence=0.5)
    )
    first_id = first[0]["event_id"]
    assert first[0]["result"] == "draft"
    async with await h.client(admin) as client:
        queue = await client.get(f"{ADMIN_EVENTS}/review-queue")
        assert [item["id"] for item in queue.json()] == [first_id]
        published = await client.post(f"{ADMIN_EVENTS}/{first_id}/publish")
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"
        assert published.json()["formation"] is not None
    second = await import_results(h, import_item(h, source_id="s2", title="py days"))
    assert second[0]["result"] == "draft"
    assert second[0]["review_reasons"] == ["possible_duplicate"]
    second_id = second[0]["event_id"]
    merged = await post(h, admin, f"{ADMIN_EVENTS}/{second_id}/merge", {"into": first_id})
    assert merged.status_code == 200, merged.text
    assert merged.json()["id"] == first_id
    assert (await load(h, UUID(second_id))).status == "archived"
    async with await h.client(member) as client:
        listing = await client.get("/api/v1/events")
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["items"]] == [first_id]
    async with await h.client(admin) as client:
        queue = await client.get(f"{ADMIN_EVENTS}/review-queue")
    assert queue.json() == []
