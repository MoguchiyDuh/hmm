import itertools
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_algo.testing import FakeAlgorithm
from hmm_contracts import algo as a
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    ConfirmationFactType,
    ConfirmationSource,
    FlagStatus,
    FlagSubjectType,
    HelpRequestFormat,
    HelpRequestStatus,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    OnboardingStatus,
    RestrictionKind,
    ReviewContext,
    ReviewSubjectType,
    Severity,
    Trust,
)
from hmm_contracts.fixtures import CommunityCase
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.platform.jobs import JOBS, REGISTRY, JobName, defer_job
from hmm_core.scores import community
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

FIXTURES = Path(__file__).resolve().parents[2] / "contracts/fixtures/community"
RING_REASON = "Repeated confirmations within a closed account cluster"

_github_ids = itertools.count(7_400_000)


def load_case(name: str) -> CommunityCase:
    return CommunityCase.model_validate_json((FIXTURES / name).read_text())


def member_row(user_id: UUID | None = None) -> models.User:
    number = next(_github_ids)
    return models.User(
        id=user_id or uuid4(),
        github_id=number,
        github_login=f"member{number}",
        display_name=f"Member {number}",
        onboarding_status=OnboardingStatus.COMPLETED,
    )


def event_row(event_id: UUID, now: datetime, **fields: Any) -> models.Event:
    return models.Event(
        id=event_id,
        slug=f"event-{event_id}",
        kind="meetup",
        formation_enabled=False,
        title="Meetup",
        starts_at=fields.pop("starts_at", now - timedelta(days=3)),
        ends_at=fields.pop("ends_at", now - timedelta(days=2)),
        timezone="UTC",
        format="offline",
        language=[],
        themes=[],
        status="published",
        review_reasons=[],
        origin=fields.pop("origin", "manual"),
        unmapped_areas=[],
        **fields,
    )


def attendance_row(
    event_id: UUID, user_id: UUID, status: AttendanceStatus, now: datetime, **fields: Any
) -> models.EventAttendance:
    return models.EventAttendance(
        event_id=event_id,
        user_id=user_id,
        status=status,
        role=AttendanceRole.ATTENDEE,
        proof_links=[],
        trust=Trust.SELF,
        status_at=now,
        **fields,
    )


def confirmation_row(
    fact_id: UUID, subject_user_id: UUID, confirmer_id: UUID, **fields: Any
) -> models.Confirmation:
    return models.Confirmation(
        fact_type=fields.pop("fact_type", ConfirmationFactType.ATTENDANCE),
        fact_id=fact_id,
        subject_user_id=subject_user_id,
        confirmer_id=confirmer_id,
        source=fields.pop("source", ConfirmationSource.PEER),
        ignored=fields.pop("ignored", False),
        **fields,
    )


async def add_mentorship(
    session: AsyncSession,
    area_id: UUID,
    mentor_id: UUID,
    learner_id: UUID,
    started_at: datetime,
    **fields: Any,
) -> models.Mentorship:
    request = models.HelpRequest(
        learner_id=learner_id,
        area_id=area_id,
        topic="Learn",
        goal="Practice",
        format=HelpRequestFormat.ONLINE,
        status=HelpRequestStatus.MATCHED,
        expires_at=started_at + timedelta(days=7),
    )
    session.add(request)
    await session.flush()
    mentorship = models.Mentorship(
        request_id=request.id,
        mentor_id=mentor_id,
        learner_id=learner_id,
        area_id=area_id,
        kind=MentorshipKind.MENTORING,
        mentor_score_at_start=Decimal("8.00"),
        learner_score_at_start=Decimal("3.00"),
        status=fields.pop("status", MentorshipStatus.ACTIVE),
        started_at=started_at,
        mentor_trust=Trust.PEER,
        learner_trust=Trust.SELF,
        **fields,
    )
    session.add(mentorship)
    await session.flush()
    return mentorship


def review_row(
    reviewer_id: UUID, reviewee_id: UUID, subject_id: UUID, **fields: Any
) -> models.Review:
    return models.Review(
        subject_type=ReviewSubjectType.EVENT,
        subject_id=subject_id,
        reviewer_id=reviewer_id,
        reviewee_id=reviewee_id,
        context=fields.pop("context", ReviewContext.MEMBER),
        rating=fields.pop("rating", 8),
        comment="Solid",
        **fields,
    )


async def seed_case(h: Harness, case: CommunityCase) -> None:
    edges = case.graph.confirmations
    users = sorted(
        {edge.subject_user_id for edge in edges}
        | {edge.confirmer_id for edge in edges if edge.confirmer_id is not None}
    )
    async with h.db() as session:
        session.add_all(member_row(user_id) for user_id in users)
        await session.flush()
        session.add_all(
            event_row(context, h.now) for context in sorted({e.context_id for e in edges})
        )
        await session.flush()
        session.add_all(
            attendance_row(
                edge.context_id,
                edge.subject_user_id,
                AttendanceStatus.ATTENDED,
                h.now,
                id=edge.fact_id,
            )
            for edge in edges
        )
        await session.flush()
        session.add_all(
            models.Confirmation(
                id=edge.id,
                fact_type=edge.fact_type,
                fact_id=edge.fact_id,
                subject_user_id=edge.subject_user_id,
                confirmer_id=edge.confirmer_id,
                source=edge.source,
                ignored=edge.ignored,
                withdrawn_at=edge.withdrawn_at,
                created_at=edge.created_at,
            )
            for edge in edges
        )


async def scan(h: Harness) -> int:
    async with h.db() as session:
        return await community.scan_community(session, h.runtime)


async def graph(h: Harness) -> a.CommunityGraph:
    async with h.db() as session:
        return await community.build_community_graph(session, h.now)


async def flag_rows(h: Harness) -> list[models.Flag]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Flag).order_by(models.Flag.created_at, models.Flag.id)
            )
        )


async def alerts(h: Harness) -> list[models.AdminAlert]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.AdminAlert)
                .where(models.AdminAlert.type == AlertKind.SUSPICIOUS_ACTIVITY)
                .order_by(models.AdminAlert.dedupe_key)
            )
        )


def ring_members(case: CommunityCase) -> list[UUID]:
    return sorted(next(iter(case.expect_flagged)))


async def test_ring_flags_once_per_subject(harness: Harness) -> None:
    h = harness
    case = load_case("mutual_confirmation_ring.json")
    await seed_case(h, case)
    users = ring_members(case)
    assert await scan(h) == 15
    rows = await flag_rows(h)
    assert len(rows) == 15
    assert {row.subject_id for row in rows} == {edge.id for edge in case.graph.confirmations}
    for row in rows:
        assert row.status == FlagStatus.TO_CHECK
        assert row.subject_type == FlagSubjectType.CONFIRMATION
        assert row.related_user_ids == users
        assert row.reason == RING_REASON
    raised = await alerts(h)
    assert sorted(alert.dedupe_key or "" for alert in raised) == sorted(
        f"suspicious_activity:{user_id}:{h.now.date().isoformat()}" for user_id in users
    )
    pending = await h.pending_jobs("recompute_member")
    assert sorted(job["args"]["user_id"] for job in pending) == sorted(str(u) for u in users)
    assert await scan(h) == 15
    assert len(await flag_rows(h)) == 15
    assert len(await alerts(h)) == 3
    assert len(await h.pending_jobs("recompute_member")) == 3


async def test_pardoned_flag_is_not_raised_again(harness: Harness) -> None:
    h = harness
    case = load_case("mutual_confirmation_ring.json")
    await seed_case(h, case)
    admin = await h.admin()
    assert await scan(h) == 15
    async with h.db() as session:
        pardoned = await session.scalar(
            select(models.Flag).order_by(models.Flag.subject_id).limit(1)
        )
        assert pardoned is not None
        pardoned.status = FlagStatus.PARDONED
        pardoned.resolved_by = admin.id
        pardoned.resolved_at = h.now
        subject_id = pardoned.subject_id
    assert await scan(h) == 14
    rows = await flag_rows(h)
    same = [row for row in rows if row.subject_id == subject_id]
    assert [row.status for row in same] == [FlagStatus.PARDONED]
    assert len([row for row in rows if row.status == FlagStatus.TO_CHECK]) == 14


async def test_clean_meeting_is_not_flagged(harness: Harness) -> None:
    h = harness
    case = load_case("clean.json")
    await seed_case(h, case)
    assert len({edge.context_id for edge in case.graph.confirmations}) == 1
    assert await scan(h) == 0
    assert await flag_rows(h) == []
    assert await alerts(h) == []
    assert await h.pending_jobs("recompute_member") == []


async def test_graph_content(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    area = next(iter(areas.values()))
    creator, guest, outsider, mentor, learner = [await h.user() for _ in range(5)]
    async with h.db() as session:
        event = event_row(uuid4(), h.now, origin="member", created_by=creator.id, created_at=h.now)
        session.add(event)
        await session.flush()
        attended = attendance_row(event.id, creator.id, AttendanceStatus.ATTENDED, h.now)
        going = attendance_row(event.id, guest.id, AttendanceStatus.GOING, h.now)
        cancelled = attendance_row(event.id, outsider.id, AttendanceStatus.CANCELLED, h.now)
        session.add_all([attended, going, cancelled])
        await session.flush()
        confirmation = confirmation_row(
            attended.id, creator.id, guest.id, created_at=h.now - timedelta(days=1)
        )
        session.add(confirmation)
        mentorship = await add_mentorship(
            session, area.id, mentor.id, learner.id, h.now - timedelta(days=10)
        )
        review = review_row(learner.id, mentor.id, event.id, context=ReviewContext.MENTOR, rating=9)
        session.add(review)
    result = await graph(h)
    assert len(result.confirmations) == 1
    edge = result.confirmations[0]
    assert edge.id == confirmation.id
    assert edge.context_id == event.id
    assert edge.source == ConfirmationSource.PEER
    assert edge.ignored is False
    assert edge.flag_status is None
    assert [(m.id, m.area) for m in result.mentorships] == [(mentorship.id, area.key)]
    assert [(r.id, r.context, r.rating) for r in result.reviews] == [
        (review.id, ReviewContext.MENTOR, 9)
    ]
    assert len(result.member_events) == 1
    summary = result.member_events[0]
    assert summary.id == event.id
    assert summary.creator_id == creator.id
    assert summary.attendees == sorted([creator.id, guest.id])
    assert edge in summary.confirmations
    assert set(result.accounts) == {creator.id, guest.id, mentor.id, learner.id}


async def test_graph_window(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    area = next(iter(areas.values()))
    old = h.now - timedelta(days=200)
    a_user, b_user, c_user, d_user = [await h.user() for _ in range(4)]
    async with h.db() as session:
        event = event_row(
            uuid4(),
            h.now,
            origin="member",
            created_by=a_user.id,
            created_at=old,
            starts_at=old,
            ends_at=old + timedelta(hours=2),
        )
        session.add(event)
        await session.flush()
        attendance = attendance_row(event.id, a_user.id, AttendanceStatus.ATTENDED, old)
        session.add(attendance)
        await session.flush()
        session.add(confirmation_row(attendance.id, a_user.id, b_user.id, created_at=old))
        closed = await add_mentorship(
            session,
            area.id,
            a_user.id,
            b_user.id,
            old,
            status=MentorshipStatus.CLOSED,
            closed_at=old + timedelta(days=5),
            outcome=MentorshipOutcome.REACHED,
        )
        open_ = await add_mentorship(session, area.id, c_user.id, d_user.id, old)
        session.add(review_row(a_user.id, b_user.id, event.id, created_at=old))
    result = await graph(h)
    assert result.confirmations == []
    assert result.reviews == []
    assert result.member_events == []
    assert [m.id for m in result.mentorships] == [open_.id]
    assert closed.id not in {m.id for m in result.mentorships}


async def test_graph_accounts(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    subject, plain = await h.user(), await h.user()
    async with h.db() as session:
        event = event_row(uuid4(), h.now)
        session.add(event)
        await session.flush()
        attendance = attendance_row(event.id, subject.id, AttendanceStatus.ATTENDED, h.now)
        session.add(attendance)
        await session.flush()
        session.add(confirmation_row(attendance.id, subject.id, plain.id))
        session.add(
            models.MemberMetric(
                user_id=subject.id,
                xp=Decimal(30),
                activity=Decimal(0),
                algo_version="fake-1",
                input_hash="x",
                computed_at=h.now,
            )
        )
        session.add(
            models.XpAdjustment(
                user_id=subject.id, amount=Decimal(5), reason="Bonus", admin_id=admin.id
            )
        )
        session.add_all(
            [
                models.MemberRestriction(
                    user_id=subject.id,
                    kind=RestrictionKind.MENTORING,
                    until=None,
                    reason="Active",
                    created_by=admin.id,
                ),
                models.MemberRestriction(
                    user_id=subject.id,
                    kind=RestrictionKind.CONFIRMING,
                    until=h.now - timedelta(days=1),
                    reason="Expired",
                    created_by=admin.id,
                ),
            ]
        )
        session.add(
            models.GithubSnapshot(
                user_id=subject.id,
                fetched_at=h.now,
                status="ok",
                profile={
                    "login": "x",
                    "account_created_at": (h.now - timedelta(days=400)).isoformat(),
                },
                repos=[],
                languages={},
                stats={"followers": 0, "contributions_last_year": 0, "public_repos": 0},
            )
        )
    result = await graph(h)
    info = result.accounts[subject.id]
    assert info.xp == 35.0
    assert info.restrictions == [RestrictionKind.MENTORING]
    assert info.github_account_age == timedelta(days=400)
    assert result.accounts[plain.id].github_account_age is None
    assert result.accounts[plain.id].xp == 0.0
    assert list(result.accounts) == sorted(result.accounts, key=str)


class StrangerAlgorithm(FakeAlgorithm):
    async def scan_community(self, ctx: a.AlgoContext, graph: a.CommunityGraph) -> list[a.Flag]:
        return [
            a.Flag(
                subject_type=FlagSubjectType.PROFILE,
                subject_id=uuid4(),
                related_user_ids=[uuid4()],
                reason="Unknown account",
                severity=Severity.LOW,
            )
        ]


class AttendanceAlgorithm(FakeAlgorithm):
    def __init__(self, attendance_id: UUID) -> None:
        self.attendance_id = attendance_id

    async def scan_community(self, ctx: a.AlgoContext, graph: a.CommunityGraph) -> list[a.Flag]:
        return [
            a.Flag(
                subject_type=FlagSubjectType.ATTENDANCE,
                subject_id=self.attendance_id,
                related_user_ids=[],
                reason="Odd attendance",
                severity=Severity.MEDIUM,
            )
        ]


async def test_invalid_output_is_rejected(harness: Harness) -> None:
    h = harness
    owner = await h.user()
    async with h.db() as session:
        event = event_row(uuid4(), h.now)
        session.add(event)
        await session.flush()
        attendance = attendance_row(event.id, owner.id, AttendanceStatus.ATTENDED, h.now)
        session.add(attendance)
    runtime = replace(h.runtime, algorithm=StrangerAlgorithm())
    with pytest.raises(ValueError):
        async with h.db() as session:
            await community.scan_community(session, runtime)
    assert await flag_rows(h) == []
    assert await alerts(h) == []
    runtime = replace(h.runtime, algorithm=AttendanceAlgorithm(attendance.id))
    async with h.db() as session:
        assert await community.scan_community(session, runtime) == 1
    rows = await flag_rows(h)
    assert [(row.subject_id, row.user_id) for row in rows] == [(attendance.id, owner.id)]
    raised = await alerts(h)
    assert [alert.subject_id for alert in raised] == [owner.id]


async def test_job_wiring(harness: Harness) -> None:
    h = harness
    assert JobName.SCAN_COMMUNITY in REGISTRY
    spec = JOBS[JobName.SCAN_COMMUNITY]
    assert spec.cron == "0 2 * * *"
    assert spec.timeout == 1800
    async with h.db() as session:
        await defer_job(session, h.jobs, JobName.SCAN_COMMUNITY)
    pending = await h.pending_jobs("scan_community")
    assert len(pending) == 1
    assert pending[0]["status"] == "todo"
    assert pending[0]["lock"] == pending[0]["queueing_lock"] == "scan_community"
    assert pending[0]["args"] == {}
    await h.run_jobs()
    assert [job["status"] for job in await h.pending_jobs("scan_community")] == ["succeeded"]
    assert await flag_rows(h) == []
