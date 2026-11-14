from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_core import models
from hmm_core.feed.timeline import (
    hide_feed_subject,
    record_event_going,
    record_help_request,
    record_mentorship_completed,
    record_project_recruiting,
    record_project_shipped,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def project(*, status: str = "idea", visibility: str = "public") -> models.Project:
    return models.Project(
        name="Feed project",
        description_md="Project description",
        goal="Ship the project",
        links=[],
        visibility=visibility,
        status=status,
        origin="standalone",
        recruiting={"areas": ["backend"], "note": "x"},
    )


def event(h: Harness, *, status: str = "published") -> models.Event:
    return models.Event(
        slug=f"event-{uuid4()}",
        kind="meetup",
        formation_enabled=False,
        title="Feed meetup",
        starts_at=h.now + timedelta(days=3),
        ends_at=h.now + timedelta(days=3, hours=2),
        timezone="UTC",
        language=[],
        themes=[],
        status=status,
        review_reasons=[],
        origin="manual",
        unmapped_areas=[],
    )


def attendance(
    h: Harness, event_id: UUID, user_id: UUID, *, status: str = "going"
) -> models.EventAttendance:
    return models.EventAttendance(
        event_id=event_id,
        user_id=user_id,
        status=status,
        role="attendee",
        proof_links=[],
        trust="self",
        status_at=h.now,
    )


async def items(
    session: AsyncSession, subject_id: UUID, kind: str | None = None
) -> list[models.FeedItem]:
    statement = select(models.FeedItem).where(models.FeedItem.subject_id == subject_id)
    if kind is not None:
        statement = statement.where(models.FeedItem.kind == kind)
    return list(
        await session.scalars(statement.order_by(models.FeedItem.created_at, models.FeedItem.id))
    )


async def test_recruiting_retries_replaces_changes_and_hides(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    actor = await h.user()
    other_actor = await h.user()
    async with h.db() as session:
        row = project()
        session.add(row)
        await session.flush()
        area_ids = sorted([areas["backend"].id, areas["frontend"].id])
        session.add_all(
            models.ProjectArea(project_id=row.id, area_id=area_id) for area_id in reversed(area_ids)
        )
        await record_project_recruiting(session, row, actor.id, h.now)
        first = (await items(session, row.id))[0]
        assert first.kind == "project_recruiting"
        assert first.subject_type == "project"
        assert first.actor_id == actor.id
        assert first.area_ids == area_ids
        assert first.created_at == h.now
        assert first.hidden_at is None
        assert first.payload == {
            "project_id": str(row.id),
            "name": row.name,
            "goal": row.goal,
        }
        h.advance(timedelta(hours=1))
        await record_project_recruiting(session, row, other_actor.id, h.now)
        assert [item.id for item in await items(session, row.id)] == [first.id]
        assert first.actor_id == actor.id
        row.goal = "Updated goal"
        await record_project_recruiting(session, row, other_actor.id, h.now)
        changed = await items(session, row.id)
        assert len(changed) == 2
        assert changed[0].hidden_at == h.now
        assert changed[1].payload == {
            "project_id": str(row.id),
            "name": row.name,
            "goal": row.goal,
        }
        area = await session.get(models.ProjectArea, (row.id, area_ids[0]))
        assert area is not None
        await session.delete(area)
        h.advance(timedelta(hours=1))
        await record_project_recruiting(session, row, actor.id, h.now)
        changed = await items(session, row.id)
        assert len(changed) == 3
        assert changed[1].hidden_at == h.now
        assert changed[2].area_ids == [area_ids[1]]
        row.recruiting = None
        h.advance(timedelta(hours=1))
        await record_project_recruiting(session, row, actor.id, h.now)
        assert all(item.hidden_at is not None for item in await items(session, row.id))
        assert changed[2].hidden_at == h.now


@pytest.mark.parametrize(
    ("status", "visibility"),
    [("idea", "members"), ("shipped", "public"), ("active", "members")],
)
async def test_recruiting_ineligible_projects(
    harness: Harness, status: str, visibility: str
) -> None:
    h = harness
    actor = await h.user()
    async with h.db() as session:
        row = project(status=status, visibility=visibility)
        session.add(row)
        await session.flush()
        await record_project_recruiting(session, row, actor.id, h.now)
        assert await items(session, row.id) == []


async def test_shipped_deduplicates_releases_and_result_driven_items(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    actor = await h.user()
    async with h.db() as session:
        row = project(status="shipped")
        session.add(row)
        await session.flush()
        area_ids = sorted([areas["backend"].id, areas["frontend"].id])
        session.add_all(
            models.ProjectArea(project_id=row.id, area_id=area_id) for area_id in reversed(area_ids)
        )
        releases = [
            models.ProjectRelease(
                project_id=row.id, title=title, notes_md="Release notes", created_by=actor.id
            )
            for title in ("First release", "Second release")
        ]
        session.add_all(releases)
        await session.flush()
        for release in [*releases, None]:
            await record_project_shipped(session, row, release, actor.id, h.now)
            await record_project_shipped(session, row, release, actor.id, h.now)
        recorded = await items(session, row.id)
        assert len(recorded) == 3
        for item, release in zip(recorded, [*releases, None], strict=True):
            assert item.kind == "project_shipped"
            assert item.subject_type == "project"
            assert item.actor_id == actor.id
            assert item.area_ids == area_ids
            assert item.created_at == h.now
            assert item.hidden_at is None
            assert item.payload == {
                "project_id": str(row.id),
                "name": row.name,
                "release_id": str(release.id) if release is not None else None,
                "release_title": release.title if release is not None else None,
            }


async def test_shipped_requires_shipped_status(harness: Harness) -> None:
    h = harness
    actor = await h.user()
    async with h.db() as session:
        row = project(status="active")
        session.add(row)
        await session.flush()
        await record_project_shipped(session, row, None, actor.id, h.now)
        assert await items(session, row.id) == []


@pytest.mark.parametrize("status", ["open", "direct", "matched"])
async def test_help_request_open_only_and_hiding(harness: Harness, status: str) -> None:
    h = harness
    areas = await h.seed_areas()
    learner = await h.user()
    async with h.db() as session:
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=areas["backend"].id,
            topic="Database transactions",
            goal="Understand isolation",
            format="online",
            status=status,
            expires_at=h.now + timedelta(days=7),
        )
        session.add(request)
        await session.flush()
        await record_help_request(session, request, learner.id, h.now)
        await record_help_request(session, request, learner.id, h.now)
        recorded = await items(session, request.id)
        if status != "open":
            assert recorded == []
            return
        assert len(recorded) == 1
        item = recorded[0]
        assert item.kind == "help_request"
        assert item.subject_type == "help_request"
        assert item.actor_id == learner.id
        assert item.area_ids == [request.area_id]
        assert item.created_at == h.now
        assert item.hidden_at is None
        assert item.payload == {
            "request_id": str(request.id),
            "area_key": "backend",
            "topic": request.topic,
            "goal": request.goal,
            "learner_id": str(learner.id),
        }
        request.status = "cancelled"
        h.advance(timedelta(hours=1))
        assert await hide_feed_subject(session, "help_request", request.id, h.now) == 1
        assert item.hidden_at == h.now


async def test_going_aggregates_daily_and_announces_once(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    first = await h.user()
    second = await h.user()
    dnd = await h.user(dnd_indefinite=True)
    async with h.db() as session:
        row = event(h)
        session.add(row)
        await session.flush()
        area_ids = sorted([areas["backend"].id, areas["frontend"].id])
        session.add_all(
            models.EventArea(event_id=row.id, area_id=area_id, importance="primary")
            for area_id in reversed(area_ids)
        )
        session.add(attendance(h, row.id, first.id))
        await record_event_going(session, row.id, first.id, h.now)
        going = (await items(session, row.id, "event_going"))[0]
        announcement = (await items(session, row.id, "event_new"))[0]
        assert going.payload == {"event_id": str(row.id), "event_title": row.title, "count": 1}
        assert going.actor_id == first.id
        assert going.area_ids == area_ids
        assert going.subject_type == "event"
        assert going.created_at == h.now
        assert going.hidden_at is None
        assert announcement.payload == {"event_id": str(row.id), "event_title": row.title}
        assert announcement.actor_id == first.id
        assert announcement.area_ids == area_ids
        assert announcement.created_at == h.now
        assert announcement.hidden_at is None
        session.add(attendance(h, row.id, second.id))
        h.advance(timedelta(hours=1))
        row.title = "Updated meetup"
        area = await session.get(models.EventArea, (row.id, area_ids[0]))
        assert area is not None
        await session.delete(area)
        await record_event_going(session, row.id, second.id, h.now)
        assert len(await items(session, row.id, "event_going")) == 1
        assert going.payload == {"event_id": str(row.id), "event_title": row.title, "count": 2}
        assert going.actor_id == first.id
        assert going.area_ids == area_ids
        session.add(attendance(h, row.id, dnd.id))
        await record_event_going(session, row.id, dnd.id, h.now)
        assert isinstance(going.payload, dict)
        assert going.payload["count"] == 2
        assert len(await items(session, row.id, "event_new")) == 1
        announcement.hidden_at = h.now
        h.advance(timedelta(days=1))
        await record_event_going(session, row.id, second.id, h.now)
        await record_event_going(session, row.id, second.id, h.now)
        recorded = await items(session, row.id, "event_going")
        assert len(recorded) == 2
        assert recorded[1].created_at == h.now
        assert recorded[1].actor_id == second.id
        assert recorded[1].area_ids == [area_ids[1]]
        assert recorded[1].payload == going.payload
        assert len(await items(session, row.id, "event_new")) == 1


@pytest.mark.parametrize(
    ("member_state", "expected"),
    [
        ("banned", 0),
        ("indefinite", 0),
        ("future", 0),
        ("expired", 1),
        ("boundary", 1),
        ("attended", 0),
    ],
)
async def test_going_count_excludes_banned_dnd_and_other_statuses(
    harness: Harness, member_state: str, expected: int
) -> None:
    h = harness
    member = await h.user(
        banned_at=h.now if member_state == "banned" else None,
        dnd_indefinite=member_state == "indefinite",
        dnd_until=(
            h.now + timedelta(days=2)
            if member_state == "future"
            else h.now - timedelta(seconds=1)
            if member_state == "expired"
            else h.now
            if member_state == "boundary"
            else None
        ),
    )
    async with h.db() as session:
        row = event(h)
        session.add(row)
        await session.flush()
        session.add(
            attendance(
                h, row.id, member.id, status="attended" if member_state == "attended" else "going"
            )
        )
        await record_event_going(session, row.id, member.id, h.now)
        item = (await items(session, row.id, "event_going"))[0]
        assert isinstance(item.payload, dict)
        assert item.payload["count"] == expected


async def test_going_missing_or_unpublished_event_creates_nothing(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        row = event(h, status="draft")
        session.add(row)
        await session.flush()
        await record_event_going(session, row.id, member.id, h.now)
        await record_event_going(session, uuid4(), member.id, h.now)
        assert list(await session.scalars(select(models.FeedItem))) == []


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("closed", "reached"), ("cancelled", "reached"), ("closed", "partial")],
)
async def test_mentorship_completed_names_sides_and_deduplicates_hidden(
    harness: Harness, status: str, outcome: str
) -> None:
    h = harness
    areas = await h.seed_areas()
    mentor = await h.user(display_name="Mentor Name")
    learner = await h.user(display_name="Learner Name")
    async with h.db() as session:
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=areas["backend"].id,
            topic="Transactions",
            goal="Learn transactions",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=7),
        )
        session.add(request)
        await session.flush()
        row = models.Mentorship(
            request_id=request.id,
            mentor_id=mentor.id,
            learner_id=learner.id,
            area_id=request.area_id,
            kind="mentoring",
            mentor_score_at_start=Decimal("8"),
            learner_score_at_start=Decimal("3"),
            status=status,
            started_at=h.now - timedelta(days=7),
            mentor_trust="self",
            learner_trust="self",
            outcome=outcome,
        )
        session.add(row)
        await session.flush()
        await record_mentorship_completed(session, row.id, h.now)
        await record_mentorship_completed(session, row.id, h.now)
        recorded = await items(session, row.id)
        if status != "closed" or outcome != "reached":
            assert recorded == []
            return
        assert len(recorded) == 1
        item = recorded[0]
        assert item.kind == "mentorship_completed"
        assert item.subject_type == "mentorship"
        assert item.actor_id is None
        assert item.area_ids == [request.area_id]
        assert item.created_at == h.now
        assert item.hidden_at is None
        assert item.payload == {
            "mentorship_id": str(row.id),
            "area_key": "backend",
            "mentor": {"user_id": str(mentor.id), "display_name": mentor.display_name},
            "learner": {"user_id": str(learner.id), "display_name": learner.display_name},
        }
        assert await hide_feed_subject(session, "mentorship", row.id, h.now) == 1
        await record_mentorship_completed(session, row.id, h.now)
        assert len(await items(session, row.id)) == 1


async def test_hiding_counts_rows_and_preserves_first_timestamp(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        row = event(h)
        session.add(row)
        await session.flush()
        session.add(attendance(h, row.id, member.id))
        await record_event_going(session, row.id, member.id, h.now)
        h.advance(timedelta(hours=1))
        hidden_at = h.now
        assert await hide_feed_subject(session, "project", row.id, h.now) == 0
        assert await hide_feed_subject(session, "event", uuid4(), h.now) == 0
        assert await hide_feed_subject(session, "event", row.id, h.now) == 2
        assert all(item.hidden_at == hidden_at for item in await items(session, row.id))
        h.advance(timedelta(hours=1))
        assert await hide_feed_subject(session, "event", row.id, h.now) == 0
        assert all(item.hidden_at == hidden_at for item in await items(session, row.id))
