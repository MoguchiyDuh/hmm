from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    MentorshipKind,
    ReviewContext,
    ReviewSubjectType,
    ReviewWindowSubjectType,
)
from hmm_contracts.notifications import (
    LinkAction,
    NotificationPerson,
    NotificationType,
    ReviewReceivedPayload,
    ReviewsOpenPayload,
    ReviewsPendingPayload,
)
from hmm_core import models
from hmm_core.platform.errors import ProblemError
from hmm_core.results.windows import (
    REVIEW_ACTION_LABEL,
    mentorship_review_participants,
    open_review_window,
    release_review_participants,
    result_review_participants,
    reveal_due_windows,
    run_review_timers,
    send_review_reminders,
    subject_details,
    window_participants,
)
from sqlalchemy import func, select

type ReviewProject = tuple[models.User, models.User, models.User, models.Project]


@pytest.fixture
async def review_project(harness: Harness) -> ReviewProject:
    lead = await harness.user(login="lead", display_name="Zoe")
    a = await harness.user(login="alice", display_name="Alex")
    b = await harness.user(login="bob", display_name="Alex")
    async with harness.db() as session:
        project = models.Project(
            name="Aurora",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            origin="standalone",
            lead_user_id=lead.id,
        )
        session.add(project)
        await session.flush()
    return lead, a, b, project


async def test_open_result_window_snapshots_and_notifies(
    harness: Harness, review_project: ReviewProject
) -> None:
    lead, a, b, project = review_project
    async with harness.db() as session:
        event = models.Event(
            slug="nl-2026",
            kind="hackathon",
            formation_enabled=False,
            title="NL 2026",
            starts_at=harness.now - timedelta(days=3),
            ends_at=harness.now,
            timezone="UTC",
            language=["en"],
            themes=[],
            status="published",
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        session.add(project)
        project.event_id = event.id
        project.team_status = "finished"
        session.add(
            models.ProjectResult(
                project_id=project.id,
                outcome="finished",
                member_areas={},
                participants={str(lead.id): True, str(a.id): True, str(b.id): False},
            )
        )
        await session.flush()
        assert await subject_details(
            session, subject_type=ReviewSubjectType.EVENT, subject_id=event.id
        ) == ("NL 2026", f"/events/{event.id}")
        participants = await result_review_participants(session, project_id=project.id)
        assert participants == {lead.id: ReviewContext.LEAD, a.id: ReviewContext.MEMBER}
        window = await open_review_window(
            session,
            subject_type=ReviewWindowSubjectType.PROJECT,
            subject_id=project.id,
            participants=participants,
            now=harness.now,
        )
        assert window is not None
        window_id = window.id
    async with harness.db() as session:
        window = (await session.scalars(select(models.ReviewWindow))).one()
        assert window.id == window_id
        assert window.opens_at == harness.now
        assert window.closes_at == harness.now + timedelta(days=7)
        assert window.participants == {str(lead.id): "lead", str(a.id): "member"}
        assert window_participants(window) == participants
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 2
        assert {notice.user_id for notice in notifications} == {lead.id, a.id}
        link = f"/projects/{project.id}"
        for notice in notifications:
            other = a if notice.user_id == lead.id else lead
            assert notice.type == NotificationType.REVIEWS_OPEN
            assert notice.dedupe_key == f"reviews_open:{window_id}:{notice.user_id}"
            assert ReviewsOpenPayload.model_validate(notice.payload) == ReviewsOpenPayload(
                link=link,
                actions=(LinkAction(kind="link", label_key=REVIEW_ACTION_LABEL, url=link),),
                review_window_id=window_id,
                subject_type=ReviewWindowSubjectType.PROJECT,
                subject_id=project.id,
                subject_title="Aurora",
                closes_at=harness.now + timedelta(days=7),
                to_review=(NotificationPerson(user_id=other.id, display_name=other.display_name),),
                submitted_count=0,
            )
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id is None
        assert audit.actor_kind == ActorKind.SYSTEM
        assert audit.action == AuditAction.SYSTEM_TRANSITION
        assert audit.entity_type == AuditEntityType.REVIEW
        assert audit.entity_id == str(window_id)
        assert audit.after == {
            "subject_type": "project",
            "subject_id": str(project.id),
            "opens_at": harness.now.isoformat(),
            "closes_at": (harness.now + timedelta(days=7)).isoformat(),
        }
    async with harness.db() as session:
        existing = await open_review_window(
            session,
            subject_type=ReviewWindowSubjectType.PROJECT,
            subject_id=project.id,
            participants={b.id: ReviewContext.MEMBER},
            now=harness.now + timedelta(hours=1),
        )
        assert existing is not None
        assert existing.id == window_id
        assert existing.participants == {str(lead.id): "lead", str(a.id): "member"}
        assert existing.opens_at == harness.now
        assert existing.closes_at == harness.now + timedelta(days=7)
        assert await session.scalar(select(func.count()).select_from(models.ReviewWindow)) == 1
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 1


async def test_open_solo_window_does_nothing(
    harness: Harness, review_project: ReviewProject
) -> None:
    lead, _, _, project = review_project
    async with harness.db() as session:
        assert (
            await open_review_window(
                session,
                subject_type=ReviewWindowSubjectType.PROJECT,
                subject_id=project.id,
                participants={lead.id: ReviewContext.LEAD},
                now=harness.now,
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(models.ReviewWindow)) == 0
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_release_participants_use_tenure_and_live_confirmed_members(
    harness: Harness, review_project: ReviewProject
) -> None:
    lead, a, b, project = review_project
    old_member = await harness.user(login="old")
    future_departure = await harness.user(login="future")
    proposed = await harness.user(login="proposed")
    departed_now = await harness.user(login="departed-now")
    async with harness.db() as session:
        for user, role, joined_at, left_at, state in (
            (lead, "member", harness.now - timedelta(days=3), None, "confirmed"),
            (
                a,
                "member",
                harness.now - timedelta(days=3) + timedelta(seconds=1),
                None,
                "confirmed",
            ),
            (
                b,
                "member",
                harness.now - timedelta(days=10),
                harness.now - timedelta(days=1),
                "confirmed",
            ),
            (old_member, "lead", harness.now - timedelta(days=10), None, "confirmed"),
            (
                future_departure,
                "member",
                harness.now - timedelta(days=10),
                harness.now + timedelta(seconds=1),
                "confirmed",
            ),
            (proposed, "member", harness.now - timedelta(days=10), None, "proposed"),
            (departed_now, "member", harness.now - timedelta(days=10), harness.now, "confirmed"),
        ):
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    user_id=user.id,
                    role=role,
                    state=state,
                    trust="peer",
                    joined_via="invite",
                    is_team_row=False,
                    joined_at=joined_at,
                    left_at=left_at,
                    confirm_by=harness.now + timedelta(days=1) if state == "proposed" else None,
                    confirm_kind="placement" if state == "proposed" else None,
                )
            )
        await session.flush()
        assert await release_review_participants(
            session, project_id=project.id, now=harness.now
        ) == {
            lead.id: ReviewContext.LEAD,
            old_member.id: ReviewContext.LEAD,
            future_departure.id: ReviewContext.MEMBER,
        }


@pytest.mark.parametrize(
    ("kind", "context"),
    (
        (MentorshipKind.MENTORING, ReviewContext.MENTOR),
        (MentorshipKind.STUDY_PAIR, ReviewContext.MEMBER),
    ),
)
async def test_mentorship_participants_and_window(
    harness: Harness,
    review_project: ReviewProject,
    kind: MentorshipKind,
    context: ReviewContext,
) -> None:
    mentor, learner, _, _ = review_project
    areas = await harness.seed_areas()
    area = next(iter(areas.values()))
    async with harness.db() as session:
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=area.id,
            topic="ML basics",
            goal="g",
            format="any",
            status="matched",
            expires_at=harness.now + timedelta(days=7),
        )
        session.add(request)
        await session.flush()
        mentorship = models.Mentorship(
            request_id=request.id,
            mentor_id=mentor.id,
            learner_id=learner.id,
            area_id=area.id,
            kind=kind,
            mentor_score_at_start=Decimal("5.00"),
            learner_score_at_start=Decimal("3.00"),
            status="closed",
            started_at=harness.now - timedelta(days=14),
            mentor_trust="peer",
            learner_trust="peer",
        )
        session.add(mentorship)
        await session.flush()
        participants = await mentorship_review_participants(session, mentorship_id=mentorship.id)
        assert participants == {mentor.id: context, learner.id: context}
        assert await subject_details(
            session, subject_type=ReviewSubjectType.MENTORSHIP, subject_id=mentorship.id
        ) == ("ML basics", f"/mentorships/{mentorship.id}")
        window = await open_review_window(
            session,
            subject_type=ReviewWindowSubjectType.MENTORSHIP,
            subject_id=mentorship.id,
            participants=participants,
            now=harness.now,
        )
        assert window is not None
        assert window_participants(window) == participants
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        for notice in notices:
            payload = ReviewsOpenPayload.model_validate(notice.payload)
            assert payload.subject_type == ReviewWindowSubjectType.MENTORSHIP
            assert payload.subject_title == "ML basics"
            assert payload.link == f"/mentorships/{mentorship.id}"


async def test_missing_subjects_and_result(harness: Harness, review_project: ReviewProject) -> None:
    _, _, _, project = review_project
    async with harness.db() as session:
        with pytest.raises(ProblemError) as error:
            await mentorship_review_participants(session, mentorship_id=uuid4())
        assert error.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(ProblemError) as error:
            await release_review_participants(session, project_id=uuid4(), now=harness.now)
        assert error.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(ProblemError) as error:
            await result_review_participants(session, project_id=project.id)
        assert error.value.code == ErrorCode.INVALID_STATE
        assert error.value.detail == "Project has no result"
        for subject_type in ReviewSubjectType:
            with pytest.raises(ProblemError) as error:
                await subject_details(session, subject_type=subject_type, subject_id=uuid4())
            assert error.value.code == ErrorCode.NOT_FOUND


async def test_reveal_due_windows_groups_live_reviews_and_endorsements(
    harness: Harness, review_project: ReviewProject
) -> None:
    lead, a, b, project = review_project
    async with harness.db() as session:
        window = models.ReviewWindow(
            subject_type="project",
            subject_id=project.id,
            opens_at=harness.now - timedelta(days=8),
            closes_at=harness.now - timedelta(days=1),
            participants={str(lead.id): "lead", str(a.id): "member", str(b.id): "member"},
        )
        future_window = models.ReviewWindow(
            subject_type="project",
            subject_id=project.id,
            opens_at=harness.now,
            closes_at=harness.now + timedelta(days=7),
            participants={},
        )
        session.add_all([window, future_window])
        await session.flush()
        for reviewer, reviewee, context, rating, removed_at in (
            (lead, a, "member", 8, None),
            (a, lead, "lead", None, None),
            (b, a, "member", 9, None),
            (b, lead, "lead", 1, harness.now - timedelta(days=2)),
        ):
            session.add(
                models.Review(
                    window_id=window.id,
                    subject_type="project",
                    subject_id=project.id,
                    reviewer_id=reviewer.id,
                    reviewee_id=reviewee.id,
                    context=context,
                    rating=rating,
                    comment="Worked together",
                    removed_at=removed_at,
                )
            )
        await session.flush()
        window_id = window.id
        assert await reveal_due_windows(session, harness.now) == 1
    async with harness.db() as session:
        window = await session.get(models.ReviewWindow, window_id)
        assert window is not None
        assert window.revealed_at == harness.now
        future = await session.get(models.ReviewWindow, future_window.id)
        assert future is not None
        assert future.revealed_at is None
        reviews = list(await session.scalars(select(models.Review)))
        assert len(reviews) == 4
        assert all(review.revealed_at == harness.now for review in reviews)
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {lead.id, a.id}
        for notice in notices:
            context, count = (
                (ReviewContext.LEAD, 1) if notice.user_id == lead.id else (ReviewContext.MEMBER, 2)
            )
            assert notice.type == NotificationType.REVIEW_RECEIVED
            assert (
                notice.dedupe_key == f"review_received:{window_id}:{notice.user_id}:{context.value}"
            )
            assert ReviewReceivedPayload.model_validate(notice.payload) == ReviewReceivedPayload(
                link=f"/projects/{project.id}",
                actions=(),
                review_window_id=window_id,
                subject_id=project.id,
                subject_title="Aurora",
                review_count=count,
                context=context,
                revealed_at=harness.now,
            )
        audit = (await session.scalars(select(models.AuditLog))).one()
        assert audit.actor_id is None
        assert audit.actor_kind == ActorKind.SYSTEM
        assert audit.action == AuditAction.SYSTEM_TRANSITION
        assert audit.entity_type == AuditEntityType.REVIEW
        assert audit.entity_id == str(window_id)
        assert audit.after == {"revealed_at": harness.now.isoformat()}
        assert await reveal_due_windows(session, harness.now) == 0
        await run_review_timers(session, harness.now)
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 1


async def test_review_reminders_only_notify_outstanding_peers_once(
    harness: Harness, review_project: ReviewProject
) -> None:
    lead, a, b, project = review_project
    closes_at = harness.now + timedelta(days=2)
    async with harness.db() as session:
        windows = [
            models.ReviewWindow(
                subject_type="project",
                subject_id=project.id,
                opens_at=harness.now - timedelta(days=5),
                closes_at=closes_at,
                participants={str(lead.id): "lead", str(b.id): "member", str(a.id): "member"},
            ),
            models.ReviewWindow(
                subject_type="project",
                subject_id=project.id,
                opens_at=harness.now - timedelta(days=5),
                closes_at=closes_at,
                revealed_at=harness.now,
                participants={str(lead.id): "lead", str(a.id): "member"},
            ),
            models.ReviewWindow(
                subject_type="project",
                subject_id=project.id,
                opens_at=harness.now - timedelta(days=5),
                closes_at=closes_at,
                participants={str(lead.id): "lead"},
            ),
            models.ReviewWindow(
                subject_type="project",
                subject_id=project.id,
                opens_at=harness.now - timedelta(days=7),
                closes_at=harness.now - timedelta(seconds=1),
                participants={str(lead.id): "lead", str(a.id): "member"},
            ),
        ]
        session.add_all(windows)
        await session.flush()
        window_id = windows[0].id
        for reviewer, reviewee, context in (
            (lead, a, "member"),
            (a, lead, "lead"),
            (a, b, "member"),
        ):
            session.add(
                models.Review(
                    window_id=window_id,
                    subject_type="project",
                    subject_id=project.id,
                    reviewer_id=reviewer.id,
                    reviewee_id=reviewee.id,
                    context=context,
                    rating=8,
                    comment="Helpful",
                )
            )
        await session.flush()
        assert await send_review_reminders(session, harness.now - timedelta(seconds=1)) == 0
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert await send_review_reminders(session, harness.now) == 2
    async with harness.db() as session:
        notices = list(await session.scalars(select(models.Notification)))
        assert len(notices) == 2
        assert {notice.user_id for notice in notices} == {lead.id, b.id}
        link = f"/projects/{project.id}"
        for notice in notices:
            peers, submitted_count = ((b,), 1) if notice.user_id == lead.id else ((a, lead), 0)
            assert notice.type == NotificationType.REVIEWS_PENDING
            assert notice.dedupe_key == f"reviews_pending:{window_id}:{notice.user_id}"
            assert ReviewsPendingPayload.model_validate(notice.payload) == ReviewsPendingPayload(
                link=link,
                actions=(LinkAction(kind="link", label_key=REVIEW_ACTION_LABEL, url=link),),
                review_window_id=window_id,
                subject_type=ReviewWindowSubjectType.PROJECT,
                subject_id=project.id,
                subject_title="Aurora",
                closes_at=closes_at,
                to_review=tuple(
                    NotificationPerson(user_id=peer.id, display_name=peer.display_name)
                    for peer in peers
                ),
                submitted_count=submitted_count,
            )
        assert await send_review_reminders(session, harness.now) == 0
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2
