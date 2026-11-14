from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.notifications import (
    AttendanceConfirmDigestPayload,
    ContributionsConfirmDigestPayload,
)
from hmm_core import models
from hmm_core.feed.digests import (
    send_attendance_digest,
    send_contributions_digest,
    send_pending_attendance_digests,
)
from hmm_core.platform.settings import load_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def seed_event(
    session: AsyncSession,
    now: datetime,
    *,
    ends_at: datetime | None = None,
    status: str = "published",
    creator_id: UUID | None = None,
) -> models.Event:
    end = ends_at if ends_at is not None else now - timedelta(hours=1)
    event = models.Event(
        slug=f"digest-{uuid4()}",
        kind="meetup",
        formation_enabled=False,
        title="Confirmation meetup",
        starts_at=end - timedelta(hours=2),
        ends_at=end,
        timezone="UTC",
        language=[],
        themes=[],
        status=status,
        review_reasons=[],
        origin="member" if creator_id is not None else "manual",
        created_by=creator_id,
        unmapped_areas=[],
    )
    session.add(event)
    await session.flush()
    return event


def attendance(
    event: models.Event, user: models.User, now: datetime, *, status: str = "attended"
) -> models.EventAttendance:
    return models.EventAttendance(
        event_id=event.id,
        user_id=user.id,
        status=status,
        role="attendee",
        proof_links=[],
        trust="self",
        status_at=now,
    )


def project_member(
    project: models.Project,
    user: models.User,
    now: datetime,
    *,
    lead: bool = False,
    left: bool = False,
) -> models.ProjectMember:
    return models.ProjectMember(
        project_id=project.id,
        user_id=user.id,
        role="lead" if lead else "member",
        state="confirmed",
        trust="self",
        joined_via="create" if lead else "invite",
        is_team_row=False,
        joined_at=now - timedelta(days=2),
        confirmed_at=now - timedelta(days=2),
        left_at=now - timedelta(days=1) if left else None,
    )


async def test_attendance_digest_recipients_payload_and_deduplication(
    harness: Harness,
) -> None:
    h = harness
    a = await h.user(display_name="Alpha", dnd_indefinite=True)
    b = await h.user(display_name="Beta")
    c = await h.user(display_name="Banned", banned_at=h.now)
    d = await h.user(display_name="Going")
    creator = await h.user(display_name="Creator")
    async with h.db() as session:
        event = await seed_event(session, h.now, creator_id=creator.id)
        rows = [attendance(event, user, h.now) for user in (a, b, c)]
        session.add_all([*rows, attendance(event, d, h.now, status="going")])
        await session.flush()
        assert await send_attendance_digest(session, event.id, h.now) == 3
        assert await send_attendance_digest(session, event.id, h.now) == 0
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 3
        assert {row.user_id for row in notifications} == {a.id, b.id, creator.id}
        assert {row.type for row in notifications} == {"attendance_confirm_digest"}
        payloads = {
            row.user_id: AttendanceConfirmDigestPayload.model_validate(row.payload)
            for row in notifications
        }
        payload = payloads[a.id]
        assert payload.event_id == event.id
        assert payload.event_title == event.title
        assert payload.link == f"/events/{event.id}"
        assert payload.ends_at == event.ends_at
        assert payload.attendee_count == 1
        assert [
            (item.user_id, item.display_name, item.attendance_id) for item in payload.attendees
        ] == [(b.id, b.display_name, rows[1].id)]
        assert [item.user_id for item in payloads[creator.id].attendees] == [a.id, b.id]
        assert payloads[creator.id].attendee_count == 2
        assert {row.dedupe_key for row in notifications} == {
            f"attendance_confirm_digest:{user.id}:{event.id}" for user in (a, b, creator)
        }


@pytest.mark.parametrize("status", ["published", "archived"])
async def test_attendance_digest_at_event_end(harness: Harness, status: str) -> None:
    h = harness
    users = [await h.user(), await h.user()]
    async with h.db() as session:
        event = await seed_event(session, h.now, ends_at=h.now, status=status)
        session.add_all(attendance(event, user, h.now) for user in users)
        assert await send_attendance_digest(session, event.id, h.now) == 2


@pytest.mark.parametrize(
    ("status", "end_delta", "attendee_count"),
    [
        ("published", timedelta(hours=1), 2),
        ("draft", timedelta(hours=-1), 2),
        ("cancelled", timedelta(hours=-1), 2),
        ("published", timedelta(hours=-1), 0),
        ("published", timedelta(hours=-1), 1),
    ],
)
async def test_attendance_digest_boundaries(
    harness: Harness, status: str, end_delta: timedelta, attendee_count: int
) -> None:
    h = harness
    users = [await h.user() for _ in range(attendee_count)]
    async with h.db() as session:
        event = await seed_event(session, h.now, ends_at=h.now + end_delta, status=status)
        session.add_all(attendance(event, user, h.now) for user in users)
        assert await send_attendance_digest(session, event.id, h.now) == 0
        assert await send_attendance_digest(session, uuid4(), h.now) == 0
        assert list(await session.scalars(select(models.Notification))) == []


async def test_pending_attendance_digests_claim_window_and_limit(harness: Harness) -> None:
    h = harness
    users = [await h.user(), await h.user()]
    async with h.db() as session:
        claim_days = (await load_settings(session)).attendance_claim_days
        recent = await seed_event(session, h.now, ends_at=h.now - timedelta(days=1))
        old = await seed_event(session, h.now, ends_at=h.now - timedelta(days=claim_days + 1))
        boundary = await seed_event(session, h.now, ends_at=h.now - timedelta(days=claim_days))
        cancelled = await seed_event(session, h.now, status="cancelled")
        await seed_event(session, h.now, ends_at=h.now - timedelta(days=2))
        for event in (recent, old, boundary, cancelled):
            session.add_all(attendance(event, user, h.now) for user in users)
        assert await send_pending_attendance_digests(session, h.now, limit=0) == 0
        assert await send_pending_attendance_digests(session, h.now, limit=1) == 2
        assert await send_pending_attendance_digests(session, h.now) == 0
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert all(
            AttendanceConfirmDigestPayload.model_validate(row.payload).event_id == recent.id
            for row in notifications
        )


async def test_contributions_digest_recipients_payload_and_deduplication(
    harness: Harness,
) -> None:
    h = harness
    areas = await h.seed_areas()
    lead = await h.user(display_name="Lead")
    m1 = await h.user(dnd_indefinite=True)
    m2 = await h.user()
    banned = await h.user(banned_at=h.now)
    left = await h.user()
    async with h.db() as session:
        project = models.Project(
            name="Digest project",
            description_md="Description",
            goal="Ship a release",
            links=[],
            visibility="public",
            status="active",
            origin="standalone",
            lead_user_id=lead.id,
        )
        session.add(project)
        await session.flush()
        session.add(models.ProjectArea(project_id=project.id, area_id=areas["backend"].id))
        lead_member = project_member(project, lead, h.now, lead=True)
        session.add_all(
            [
                lead_member,
                project_member(project, m1, h.now),
                project_member(project, m2, h.now),
                project_member(project, banned, h.now),
                project_member(project, left, h.now, left=True),
            ]
        )
        release = models.ProjectRelease(
            project_id=project.id,
            title="First release",
            notes_md="Release notes",
            created_by=lead.id,
        )
        session.add(release)
        await session.flush()
        assert await send_contributions_digest(session, release.id, h.now) == 2
        assert await send_contributions_digest(session, release.id, h.now) == 0
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert {row.user_id for row in notifications} == {m1.id, m2.id}
        for row in notifications:
            assert row.type == "contributions_confirm_digest"
            payload = ContributionsConfirmDigestPayload.model_validate(row.payload)
            assert payload.project_id == project.id
            assert payload.project_name == project.name
            assert payload.release_id == release.id
            assert payload.release_title == release.title
            assert payload.link == f"/projects/{project.id}"
            assert payload.lead.user_id == lead.id
            assert payload.lead.display_name == lead.display_name
            assert payload.project_member_id == lead_member.id
            assert row.dedupe_key == f"contributions_confirm_digest:{row.user_id}:{release.id}"
        lead_member.left_at = h.now
        assert await send_contributions_digest(session, release.id, h.now) == 0
        project.lead_user_id = None
        assert await send_contributions_digest(session, release.id, h.now) == 0
        assert await send_contributions_digest(session, uuid4(), h.now) == 0
