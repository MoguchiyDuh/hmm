from datetime import UTC, datetime, timedelta

import pytest
from harness import NOW, Harness
from hmm_contracts.common import AlertStatus
from hmm_contracts.notifications import AdminDigestPayload, AlertKind, NotificationType
from hmm_core import models
from hmm_core.admin.jobs import admin_digest, digest_period
from hmm_core.platform.jobs import JOBS, REGISTRY, JobName
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def add_event(
    session: AsyncSession, now: datetime, slug: str, *, origin: str, review_reasons: list[str]
) -> models.Event:
    event = models.Event(
        slug=slug,
        kind="hackathon",
        formation_enabled=True,
        title=slug,
        starts_at=now + timedelta(days=5),
        ends_at=now + timedelta(days=6),
        timezone="UTC",
        language=["en"],
        themes=[],
        status="published",
        review_reasons=review_reasons,
        origin=origin,
        unmapped_areas=[],
    )
    session.add(event)
    await session.flush()
    return event


def flag(user: models.User, created_at: datetime) -> models.Flag:
    return models.Flag(
        user_id=user.id,
        subject_type="profile",
        subject_id=user.id,
        related_user_ids=[],
        reason="Review profile",
        severity="low",
        status="to_check",
        algo_version="test",
        created_at=created_at,
    )


async def digests(h: Harness) -> list[models.Notification]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Notification)
                .where(models.Notification.type == NotificationType.ADMIN_DIGEST.value)
                .order_by(models.Notification.created_at, models.Notification.id)
            )
        )


@pytest.mark.parametrize(
    "now,start,end",
    [
        (NOW, datetime(2026, 11, 2, 9, tzinfo=UTC), datetime(2026, 11, 9, 9, tzinfo=UTC)),
        (
            datetime(2026, 11, 16, 8, tzinfo=UTC),
            datetime(2026, 11, 2, 9, tzinfo=UTC),
            datetime(2026, 11, 9, 9, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 16, 9, tzinfo=UTC),
            datetime(2026, 11, 9, 9, tzinfo=UTC),
            datetime(2026, 11, 16, 9, tzinfo=UTC),
        ),
    ],
)
def test_digest_period(now: datetime, start: datetime, end: datetime) -> None:
    assert digest_period(now) == (start, end)


async def test_admin_digest_counts_recipients_and_no_alerts(harness: Harness) -> None:
    h = harness
    admins = [await h.admin(), await h.admin()]
    banned = await h.admin(banned_at=h.now)
    member = await h.user()
    other = await h.user()
    async with h.db() as session:
        session.add(
            models.MemberReview(
                user_id=member.id, status="pending", opened_at=h.now, discrepancies=[]
            )
        )
        session.add_all(
            [flag(member, h.now - timedelta(days=8)), flag(other, h.now - timedelta(days=6))]
        )
        await add_event(
            session, h.now, "duplicate", origin="member", review_reasons=["possible_duplicate"]
        )
        event = await add_event(session, h.now, "results", origin="manual", review_reasons=[])
        project = models.Project(
            event_id=event.id,
            name="Team",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            team_status="finished",
            origin="formation",
        )
        session.add(project)
        await session.flush()
        session.add(
            models.ProjectResult(
                project_id=project.id,
                outcome="finished",
                place=1,
                place_status="unverified",
                member_areas={},
                participants=[],
            )
        )
        session.add(
            models.AdminAlert(type="review_reported", severity="info", status="open", payload={})
        )
    await admin_digest(h.runtime)
    start, end = digest_period(h.now)
    rows = await digests(h)
    assert {row.user_id for row in rows} == {actor.id for actor in admins}
    assert len(rows) == 2
    assert banned.id not in {row.user_id for row in rows}
    for row in rows:
        payload = AdminDigestPayload.model_validate(row.payload)
        assert payload.link == "/admin/dashboard"
        assert payload.actions == ()
        assert payload.period_starts_at == start
        assert payload.period_ends_at == end
        assert payload.pending_member_reviews == 1
        assert payload.overdue_to_check_items == 1
        assert payload.member_event_duplicates == 1
        assert payload.results_to_verify == 1
        assert payload.open_alerts == {
            kind: int(kind == AlertKind.REVIEW_REPORTED) for kind in AlertKind
        }
        assert isinstance(row.payload, dict)
        assert row.payload["actions"] == []
    async with h.db() as session:
        alerts = list(await session.scalars(select(models.AdminAlert)))
        assert [alert.type for alert in alerts] == ["review_reported"]
        notifications = list(await session.scalars(select(models.Notification)))
        assert {row.type for row in notifications} == {NotificationType.ADMIN_DIGEST.value}


async def test_admin_digest_is_repeat_safe_per_period(harness: Harness) -> None:
    h = harness
    admins = [await h.admin(), await h.admin()]
    await admin_digest(h.runtime)
    await admin_digest(h.runtime)
    first = await digests(h)
    assert len(first) == 2
    for row in first:
        payload = AdminDigestPayload.model_validate(row.payload)
        assert payload.pending_member_reviews == 0
        assert payload.overdue_to_check_items == 0
        assert payload.open_alerts == dict.fromkeys(AlertKind, 0)
    h.advance(timedelta(days=7))
    await admin_digest(h.runtime)
    rows = await digests(h)
    assert len(rows) == 4
    for actor in admins:
        ends = [
            AdminDigestPayload.model_validate(row.payload).period_ends_at
            for row in rows
            if row.user_id == actor.id
        ]
        assert sorted(ends) == [
            datetime(2026, 11, 9, 9, tzinfo=UTC),
            datetime(2026, 11, 16, 9, tzinfo=UTC),
        ]


async def test_admin_digest_checks_parser_down(harness: Harness) -> None:
    h = harness
    admins = [await h.admin(), await h.admin()]
    last = h.now - timedelta(hours=27)
    async with h.db() as session:
        session.add(
            models.ParserHeartbeat(
                run_id="stale",
                started_at=last - timedelta(hours=1),
                finished_at=last,
                stats={"sources_checked": 0, "candidates": 0, "imported": 0, "errors": 0},
                errors=[],
            )
        )
    for _ in range(2):
        await admin_digest(h.runtime)
    async with h.db() as session:
        alert = (await session.scalars(select(models.AdminAlert))).one()
        assert alert.type == AlertKind.PARSER_DOWN
        assert alert.status == AlertStatus.OPEN
        notifications = list(await session.scalars(select(models.Notification)))
    by_type: dict[str, set[object]] = {}
    for row in notifications:
        by_type.setdefault(row.type, set()).add(row.user_id)
    assert len(notifications) == 4
    assert by_type == {
        NotificationType.ADMIN_ALERT.value: {actor.id for actor in admins},
        NotificationType.ADMIN_DIGEST.value: {actor.id for actor in admins},
    }
    for row in notifications:
        if row.type == NotificationType.ADMIN_DIGEST.value:
            payload = AdminDigestPayload.model_validate(row.payload)
            assert payload.open_alerts[AlertKind.PARSER_DOWN] == 1


def test_admin_digest_registration() -> None:
    assert JobName.ADMIN_DIGEST in REGISTRY
    spec = JOBS[JobName.ADMIN_DIGEST]
    assert spec.cron == "0 9 * * 1"
    assert spec.lock == spec.queueing_lock == "admin_digest"
