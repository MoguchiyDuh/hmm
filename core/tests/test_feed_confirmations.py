import random
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    AuditAction,
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    Trust,
)
from hmm_core import models
from hmm_core.feed.confirmations import (
    add_confirmation,
    confirmation_candidates,
    confirmation_target,
    confirmation_view,
    fact_trust,
    recompute_fact_trust,
    refresh_confirmation,
    refresh_member_confirmations,
    withdraw_allowed,
    withdraw_confirmation,
)
from hmm_core.feed.schemas import ConfirmationAction
from hmm_core.platform.errors import ProblemError
from sqlalchemy import func, select

ATTENDANCE = ConfirmationFactType.ATTENDANCE
PROJECT_MEMBER = ConfirmationFactType.PROJECT_MEMBER
MENTORSHIP_SIDE = ConfirmationFactType.MENTORSHIP_SIDE
PEER = ConfirmationSource.PEER


@contextmanager
def problem(code: ErrorCode) -> Generator[None]:
    with pytest.raises(ProblemError) as error:
        yield
    assert error.value.code == code


async def event_facts(
    h: Harness,
    users: list[models.User],
    *,
    statuses: list[str] | None = None,
    roles: list[str] | None = None,
    origin: str = "manual",
    creator: models.User | None = None,
    ends_at: datetime | None = None,
    event_status: str = "published",
) -> list[models.EventAttendance]:
    async with h.db() as session:
        event = models.Event(
            slug=str(uuid4()),
            kind="hackathon",
            formation_enabled=False,
            title="Ended event",
            starts_at=h.now - timedelta(days=2),
            ends_at=ends_at or h.now,
            timezone="UTC",
            language=[],
            themes=[],
            status=event_status,
            review_reasons=[],
            origin=origin,
            created_by=creator.id if creator else None,
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        rows = [
            models.EventAttendance(
                event_id=event.id,
                user_id=user.id,
                status=statuses[index] if statuses else "attended",
                role=roles[index] if roles else "attendee",
                proof_links=[],
                trust="self",
                status_at=h.now,
            )
            for index, user in enumerate(users)
        ]
        session.add_all(rows)
        await session.flush()
        return rows


async def project_facts(h: Harness, users: list[models.User]) -> list[models.ProjectMember]:
    async with h.db() as session:
        project = models.Project(
            name="Project",
            description_md="Description",
            goal="Goal",
            links=[],
            visibility="public",
            status="idea",
            origin="standalone",
        )
        session.add(project)
        await session.flush()
        rows = [
            models.ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role="lead" if index == 0 else "member",
                state="confirmed",
                trust="self",
                joined_via="create",
                is_team_row=False,
                joined_at=h.now,
            )
            for index, user in enumerate(users)
        ]
        session.add_all(rows)
        await session.flush()
        return rows


async def mentorship_fact(
    h: Harness,
    mentor: models.User,
    learner: models.User,
    area_id: UUID,
    *,
    kind: str = "mentoring",
    status: str = "active",
) -> models.Mentorship:
    async with h.db() as session:
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=area_id,
            topic="Topic",
            goal="Goal",
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
            area_id=area_id,
            kind=kind,
            mentor_score_at_start=Decimal("5"),
            learner_score_at_start=Decimal("5"),
            status=status,
            started_at=h.now,
            mentor_trust="self",
            learner_trust="self",
        )
        session.add(row)
        await session.flush()
        return row


def flag(user_id: UUID, subject_type: str, subject_id: UUID) -> models.Flag:
    return models.Flag(
        user_id=user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        related_user_ids=[],
        reason="Review",
        severity="medium",
        status="to_check",
        algo_version="test",
    )


async def test_peer_confirmation_is_repeat_safe(harness: Harness) -> None:
    h = harness
    a, b, c = [await h.user() for _ in range(3)]
    facts = await event_facts(h, [a, b, c])
    async with h.db() as session:
        row = await add_confirmation(
            session,
            h.jobs,
            fact_type=ATTENDANCE,
            fact_id=facts[1].id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        repeated = await add_confirmation(
            session,
            h.jobs,
            fact_type=ATTENDANCE,
            fact_id=facts[1].id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        assert row.id == repeated.id
        assert await session.scalar(select(func.count()).select_from(models.Confirmation)) == 1
        assert (
            await fact_trust(
                session, fact_type=ATTENDANCE, fact_id=facts[1].id, subject_user_id=b.id, now=h.now
            )
            == Trust.PEER
        )
        stored = await session.get(models.EventAttendance, facts[1].id)
        assert stored is not None and stored.trust == "peer"
        target = await confirmation_target(session, a.id, ATTENDANCE, facts[1].id, h.now)
        assert target.source == PEER
        assert facts[1].id not in {
            candidate.fact_id for candidate in await confirmation_candidates(session, a.id, h.now)
        }


async def test_trusted_sources_and_invalid_confirmations(harness: Harness) -> None:
    h = harness
    a, b = await h.user(), await h.user()
    admin = await h.admin()
    facts = await event_facts(h, [a, b])
    async with h.db() as session:
        for fact, source, confirmer in (
            (facts[0], ConfirmationSource.SYSTEM, None),
            (facts[1], ConfirmationSource.ADMIN, admin.id),
        ):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=fact.id,
                subject_user_id=fact.user_id,
                confirmer_id=confirmer,
                source=source,
                now=h.now,
            )
            assert (
                await fact_trust(
                    session,
                    fact_type=ATTENDANCE,
                    fact_id=fact.id,
                    subject_user_id=fact.user_id,
                    now=h.now,
                )
                == Trust.TRUSTED
            )
        for source in (
            ConfirmationSource.LEAD,
            ConfirmationSource.LEARNER,
            ConfirmationSource.MENTOR,
        ):
            with pytest.raises(ValueError):
                await add_confirmation(
                    session,
                    h.jobs,
                    fact_type=ATTENDANCE,
                    fact_id=facts[1].id,
                    subject_user_id=b.id,
                    confirmer_id=a.id,
                    source=source,
                    now=h.now,
                )
        with pytest.raises(ValueError):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=facts[1].id,
                subject_user_id=b.id,
                confirmer_id=None,
                source=PEER,
                now=h.now,
            )
        with pytest.raises(ValueError):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=facts[1].id,
                subject_user_id=a.id,
                confirmer_id=admin.id,
                source=PEER,
                now=h.now,
            )
        with problem(ErrorCode.CANNOT_CONFIRM_OWN_FACT):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=facts[1].id,
                subject_user_id=b.id,
                confirmer_id=b.id,
                source=PEER,
                now=h.now,
            )
        with problem(ErrorCode.CANNOT_CONFIRM_OWN_FACT):
            await confirmation_target(session, b.id, ATTENDANCE, facts[1].id, h.now)
        with problem(ErrorCode.NOT_FOUND):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=uuid4(),
                subject_user_id=b.id,
                confirmer_id=a.id,
                source=PEER,
                now=h.now,
            )


async def test_attendance_candidates_require_ended_visible_coattendance(harness: Harness) -> None:
    h = harness
    a, b, c = [await h.user() for _ in range(3)]
    banned = await h.user(banned_at=h.now)
    rows = await event_facts(
        h, [a, b, c, banned], statuses=["attended", "attended", "going", "attended"]
    )
    await event_facts(h, [a, b], ends_at=h.now + timedelta(seconds=1))
    await event_facts(h, [a, b], event_status="cancelled")
    await event_facts(h, [a, b], statuses=["going", "attended"])
    async with h.db() as session:
        candidates = await confirmation_candidates(session, a.id, h.now)
        assert [(row.fact_id, row.source) for row in candidates] == [(rows[1].id, PEER)]
        with problem(ErrorCode.FORBIDDEN):
            await confirmation_target(session, c.id, ATTENDANCE, rows[1].id, h.now)
        with problem(ErrorCode.NOT_FOUND):
            await confirmation_target(session, a.id, ATTENDANCE, uuid4(), h.now)


async def test_member_event_creator_candidate_sources(harness: Harness) -> None:
    h = harness
    creator, subject = await h.user(), await h.user()
    organizer = await event_facts(h, [subject], origin="member", creator=creator)
    hosted = await event_facts(
        h, [creator, subject], origin="member", creator=creator, roles=["host", "attendee"]
    )
    external = await event_facts(h, [subject], creator=creator)
    async with h.db() as session:
        candidates = await confirmation_candidates(session, creator.id, h.now)
        assert {row.fact_id: row.source for row in candidates} == {
            organizer[0].id: ConfirmationSource.ORGANIZER,
            hosted[1].id: ConfirmationSource.HOST,
        }
        with problem(ErrorCode.FORBIDDEN):
            await confirmation_target(session, creator.id, ATTENDANCE, external[0].id, h.now)


async def test_project_sources_and_two_peer_lead_trust(harness: Harness) -> None:
    h = harness
    lead, a, b, outsider = [await h.user() for _ in range(4)]
    rows = await project_facts(h, [lead, a, b])
    async with h.db() as session:
        candidate = await confirmation_target(session, lead.id, PROJECT_MEMBER, rows[1].id, h.now)
        assert candidate.source == ConfirmationSource.LEAD
        await add_confirmation(
            session,
            h.jobs,
            fact_type=PROJECT_MEMBER,
            fact_id=rows[1].id,
            subject_user_id=a.id,
            confirmer_id=lead.id,
            source=candidate.source,
            now=h.now,
        )
        assert (
            await fact_trust(
                session,
                fact_type=PROJECT_MEMBER,
                fact_id=rows[1].id,
                subject_user_id=a.id,
                now=h.now,
            )
            == Trust.TRUSTED
        )
        first = await add_confirmation(
            session,
            h.jobs,
            fact_type=PROJECT_MEMBER,
            fact_id=rows[0].id,
            subject_user_id=lead.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        assert (
            await fact_trust(
                session,
                fact_type=PROJECT_MEMBER,
                fact_id=rows[0].id,
                subject_user_id=lead.id,
                now=h.now,
            )
            == Trust.PEER
        )
        await add_confirmation(
            session,
            h.jobs,
            fact_type=PROJECT_MEMBER,
            fact_id=rows[0].id,
            subject_user_id=lead.id,
            confirmer_id=b.id,
            source=PEER,
            now=h.now,
        )
        assert (
            await fact_trust(
                session,
                fact_type=PROJECT_MEMBER,
                fact_id=rows[0].id,
                subject_user_id=lead.id,
                now=h.now,
            )
            == Trust.TRUSTED
        )
        await withdraw_confirmation(session, h.jobs, first.id, a.id, h.now)
        assert (
            await fact_trust(
                session,
                fact_type=PROJECT_MEMBER,
                fact_id=rows[0].id,
                subject_user_id=lead.id,
                now=h.now,
            )
            == Trust.PEER
        )
        with pytest.raises(ValueError):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=PROJECT_MEMBER,
                fact_id=rows[0].id,
                subject_user_id=lead.id,
                confirmer_id=a.id,
                source=ConfirmationSource.LEAD,
                now=h.now,
            )
        with problem(ErrorCode.FORBIDDEN):
            await confirmation_target(session, outsider.id, PROJECT_MEMBER, rows[1].id, h.now)
        own_membership = await session.get(models.ProjectMember, rows[2].id)
        assert own_membership is not None
        own_membership.left_at = h.now
        assert await confirmation_candidates(session, b.id, h.now) == []


async def test_mentorship_sides_and_study_pair_sources(harness: Harness) -> None:
    h = harness
    mentor, learner, peer = [await h.user() for _ in range(3)]
    areas = await h.seed_areas()
    area_id = next(iter(areas.values())).id
    mentorship = await mentorship_fact(h, mentor, learner, area_id)
    study = await mentorship_fact(h, mentor, learner, area_id, kind="study_pair")
    async with h.db() as session:
        for subject, confirmer, source, forbidden in (
            (mentor, learner, ConfirmationSource.LEARNER, ConfirmationSource.MENTOR),
            (learner, mentor, ConfirmationSource.MENTOR, ConfirmationSource.LEARNER),
        ):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=MENTORSHIP_SIDE,
                fact_id=mentorship.id,
                subject_user_id=subject.id,
                confirmer_id=confirmer.id,
                source=source,
                now=h.now,
            )
            assert (
                await fact_trust(
                    session,
                    fact_type=MENTORSHIP_SIDE,
                    fact_id=mentorship.id,
                    subject_user_id=subject.id,
                    now=h.now,
                )
                == Trust.TRUSTED
            )
            with pytest.raises(ValueError):
                await add_confirmation(
                    session,
                    h.jobs,
                    fact_type=MENTORSHIP_SIDE,
                    fact_id=mentorship.id,
                    subject_user_id=subject.id,
                    confirmer_id=peer.id,
                    source=forbidden,
                    now=h.now,
                )
            await add_confirmation(
                session,
                h.jobs,
                fact_type=MENTORSHIP_SIDE,
                fact_id=study.id,
                subject_user_id=subject.id,
                confirmer_id=confirmer.id,
                source=PEER,
                now=h.now,
            )
            assert (
                await fact_trust(
                    session,
                    fact_type=MENTORSHIP_SIDE,
                    fact_id=study.id,
                    subject_user_id=subject.id,
                    now=h.now,
                )
                == Trust.PEER
            )
        with problem(ErrorCode.FORBIDDEN):
            await confirmation_target(session, peer.id, MENTORSHIP_SIDE, mentorship.id, h.now)
        with pytest.raises(ValueError):
            await add_confirmation(
                session,
                h.jobs,
                fact_type=MENTORSHIP_SIDE,
                fact_id=mentorship.id,
                subject_user_id=peer.id,
                confirmer_id=mentor.id,
                source=PEER,
                now=h.now,
            )


async def test_withdrawal_boundary_permissions_and_view(harness: Harness) -> None:
    h = harness
    a, b, c = [await h.user() for _ in range(3)]
    facts = await event_facts(h, [a, b, c])
    async with h.db() as session:
        rows = [
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=facts[1].id,
                subject_user_id=b.id,
                confirmer_id=confirmer.id,
                source=PEER,
                now=h.now,
            )
            for confirmer in (a, c)
        ]
    boundary = h.advance(timedelta(days=7))
    async with h.db() as session:
        row = await session.get(models.Confirmation, rows[0].id)
        assert row is not None
        assert withdraw_allowed(row, a.id, boundary, 7)
        assert confirmation_view(row, a.id, boundary, 7).allowed_actions == [
            ConfirmationAction.WITHDRAW
        ]
        assert confirmation_view(row, b.id, boundary, 7).allowed_actions == []
        with problem(ErrorCode.FORBIDDEN):
            await withdraw_confirmation(session, h.jobs, row.id, b.id, boundary)
        await withdraw_confirmation(session, h.jobs, row.id, a.id, boundary)
        assert confirmation_view(row, a.id, boundary, 7).allowed_actions == []
    h.advance(timedelta(microseconds=1))
    async with h.db() as session:
        await withdraw_confirmation(session, h.jobs, rows[0].id, a.id, h.now)
        with problem(ErrorCode.CONFIRMATION_WITHDRAW_CLOSED):
            await withdraw_confirmation(session, h.jobs, rows[1].id, c.id, h.now)
        with problem(ErrorCode.NOT_FOUND):
            await withdraw_confirmation(session, h.jobs, uuid4(), a.id, h.now)
        with problem(ErrorCode.FORBIDDEN):
            await withdraw_confirmation(session, h.jobs, rows[0].id, b.id, h.now)
        assert await refresh_confirmation(session, h.jobs, rows[0].id, h.now) is None
        assert await refresh_confirmation(session, h.jobs, uuid4(), h.now) is None


async def test_ignore_reasons_are_derived_and_overlap(harness: Harness) -> None:
    h = harness
    a, b, admin = await h.user(), await h.user(), await h.admin()
    facts = await event_facts(h, [a, b])
    async with h.db() as session:
        restriction = models.MemberRestriction(
            user_id=a.id, kind="confirming", until=None, reason="Review", created_by=admin.id
        )
        session.add(restriction)
        row = await add_confirmation(
            session,
            h.jobs,
            fact_type=ATTENDANCE,
            fact_id=facts[1].id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        assert row.ignored
        stored = await session.get(models.EventAttendance, facts[1].id)
        assert stored is not None and stored.trust == "self"
        restriction.lifted_at = h.now
        assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == [b.id]
        assert not row.ignored and stored.trust == "peer"
        first = flag(a.id, "profile", a.id)
        session.add(first)
        await session.flush()
        assert (
            await fact_trust(
                session, fact_type=ATTENDANCE, fact_id=facts[1].id, subject_user_id=b.id, now=h.now
            )
            == Trust.SELF
        )
        assert not row.ignored and stored.trust == "peer"
        assert (
            await recompute_fact_trust(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=facts[1].id,
                subject_user_id=b.id,
                now=h.now,
            )
            == Trust.SELF
        )
        assert row.ignored
        second = flag(a.id, "attendance", facts[0].id)
        restriction.lifted_at = None
        session.add(second)
        assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == []
        restriction.lifted_at = h.now
        await refresh_member_confirmations(session, h.jobs, a.id, h.now)
        assert row.ignored
        first.status = "pardoned"
        await refresh_member_confirmations(session, h.jobs, a.id, h.now)
        assert row.ignored
        second.status = "pardoned"
        assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == [b.id]
        assert not row.ignored and stored.trust == "peer"
        confirmation_flag = flag(admin.id, "confirmation", row.id)
        session.add(confirmation_flag)
        assert await refresh_confirmation(session, h.jobs, row.id, h.now) == b.id
        assert row.ignored and stored.trust == "self"
        confirmation_flag.status = "pardoned"
        await refresh_confirmation(session, h.jobs, row.id, h.now)
        assert not row.ignored
        ring = flag(admin.id, "ring", uuid4())
        ring.related_user_ids = [a.id]
        session.add(ring)
        await refresh_member_confirmations(session, h.jobs, a.id, h.now)
        assert row.ignored and stored.trust == "self"


async def test_ban_and_unban_refresh_all_subjects_and_preserve_audit(harness: Harness) -> None:
    h = harness
    a, b, c = [await h.user() for _ in range(3)]
    admin = await h.admin()
    facts = await event_facts(h, [a, b, c])
    async with h.db() as session:
        for fact in facts[1:]:
            await add_confirmation(
                session,
                h.jobs,
                fact_type=ATTENDANCE,
                fact_id=fact.id,
                subject_user_id=fact.user_id,
                confirmer_id=a.id,
                source=PEER,
                now=h.now,
            )
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/users/{a.id}/ban", json={"reason": "Abuse"})
        assert response.status_code == 200, response.text
        async with h.db() as session:
            rows = list(await session.scalars(select(models.Confirmation)))
            assert len(rows) == 2 and all(row.ignored for row in rows)
            assert list(
                await session.scalars(
                    select(models.EventAttendance.trust).where(
                        models.EventAttendance.user_id.in_([b.id, c.id])
                    )
                )
            ) == ["self", "self"]
            assert (
                await session.scalar(
                    select(func.count()).where(
                        models.AuditLog.action == AuditAction.USER_BAN,
                        models.AuditLog.entity_id == str(a.id),
                    )
                )
                == 1
            )
            assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == []
        jobs = await h.pending_jobs("recompute_member")
        assert sorted(job["args"]["user_id"] for job in jobs) == sorted([str(b.id), str(c.id)])
        response = await client.post(f"/api/v1/admin/users/{a.id}/unban")
        assert response.status_code == 200, response.text
    async with h.db() as session:
        assert all(not row.ignored for row in await session.scalars(select(models.Confirmation)))
        assert list(
            await session.scalars(
                select(models.EventAttendance.trust).where(
                    models.EventAttendance.user_id.in_([b.id, c.id])
                )
            )
        ) == ["peer", "peer"]


async def test_expired_restrictions_and_unban_keep_remaining_reasons(harness: Harness) -> None:
    h = harness
    a, b, admin = await h.user(), await h.user(), await h.admin()
    facts = await event_facts(h, [a, b])
    async with h.db() as session:
        session.add(
            models.MemberRestriction(
                user_id=a.id, kind="confirming", until=h.now, reason="Expired", created_by=admin.id
            )
        )
        row = await add_confirmation(
            session,
            h.jobs,
            fact_type=ATTENDANCE,
            fact_id=facts[1].id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        assert not row.ignored
        live_flag = flag(a.id, "profile", a.id)
        session.add(live_flag)
    async with await h.client(admin) as client:
        assert (
            await client.post(f"/api/v1/admin/users/{a.id}/ban", json={"reason": "Abuse"})
        ).status_code == 200
        assert (await client.post(f"/api/v1/admin/users/{a.id}/unban")).status_code == 200
    async with h.db() as session:
        stored = await session.get(models.Confirmation, row.id)
        assert stored is not None and stored.ignored
        stored_fact = await session.get(models.EventAttendance, facts[1].id)
        assert stored_fact is not None and stored_fact.trust == "self"


async def test_recompute_and_refresh_enqueue_only_one_job(harness: Harness) -> None:
    h = harness
    a, b = await h.user(), await h.user()
    facts = await event_facts(h, [a, b])
    async with h.db() as session:
        await add_confirmation(
            session,
            h.jobs,
            fact_type=ATTENDANCE,
            fact_id=facts[1].id,
            subject_user_id=b.id,
            confirmer_id=a.id,
            source=PEER,
            now=h.now,
        )
        for _ in range(10):
            assert (
                await recompute_fact_trust(
                    session,
                    h.jobs,
                    fact_type=ATTENDANCE,
                    fact_id=facts[1].id,
                    subject_user_id=b.id,
                    now=h.now,
                )
                == Trust.PEER
            )
        assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == []
    assert len(await h.pending_jobs("recompute_member")) == 1
    await h.run_jobs()
    before = await h.pending_jobs("recompute_member")
    async with h.db() as session:
        assert await refresh_member_confirmations(session, h.jobs, a.id, h.now) == []
        rows = list(await session.scalars(select(models.Confirmation)))
        assert len(rows) == 1
        assert await refresh_confirmation(session, h.jobs, rows[0].id, h.now) == b.id
        assert not session.dirty
    assert await h.pending_jobs("recompute_member") == before


async def test_seeded_add_withdraw_sequence_preserves_trust(harness: Harness) -> None:
    h = harness
    users = [await h.user() for _ in range(3)]
    facts = await event_facts(h, users)
    rng = random.Random(39)
    live: dict[tuple[int, int], UUID] = {}
    async with h.db() as session:
        for _ in range(45):
            confirmer, subject = rng.sample(range(3), 2)
            key = confirmer, subject
            if key in live and rng.choice([True, False]):
                await withdraw_confirmation(
                    session, h.jobs, live.pop(key), users[confirmer].id, h.now
                )
            else:
                row = await add_confirmation(
                    session,
                    h.jobs,
                    fact_type=ATTENDANCE,
                    fact_id=facts[subject].id,
                    subject_user_id=users[subject].id,
                    confirmer_id=users[confirmer].id,
                    source=PEER,
                    now=h.now,
                )
                live[key] = row.id
            for index, fact in enumerate(facts):
                expected = "peer" if any(target == index for _, target in live) else "self"
                stored = await session.get(models.EventAttendance, fact.id)
                assert stored is not None and stored.trust == expected
                assert (
                    await fact_trust(
                        session,
                        fact_type=ATTENDANCE,
                        fact_id=fact.id,
                        subject_user_id=users[index].id,
                        now=h.now,
                    )
                    == expected
                )
    assert len(await h.pending_jobs("recompute_member")) == 3


async def test_ineligible_facts_fail_without_confirmations(harness: Harness) -> None:
    h = harness
    a, b = await h.user(), await h.user()
    attendance = await event_facts(h, [a, b], statuses=["attended", "going"])
    members = await project_facts(h, [a, b])
    areas = await h.seed_areas()
    mentorship = await mentorship_fact(h, a, b, next(iter(areas.values())).id, status="cancelled")
    async with h.db() as session:
        member = await session.get(models.ProjectMember, members[1].id)
        assert member is not None
        member.state = "proposed"
        member.confirm_by = h.now + timedelta(days=1)
        member.confirm_kind = "placement"
        for fact_type, fact_id in (
            (ATTENDANCE, attendance[1].id),
            (PROJECT_MEMBER, member.id),
            (MENTORSHIP_SIDE, mentorship.id),
        ):
            with problem(ErrorCode.INVALID_STATE):
                await add_confirmation(
                    session,
                    h.jobs,
                    fact_type=fact_type,
                    fact_id=fact_id,
                    subject_user_id=b.id,
                    confirmer_id=a.id,
                    source=PEER,
                    now=h.now,
                )
        assert await session.scalar(select(func.count()).select_from(models.Confirmation)) == 0
