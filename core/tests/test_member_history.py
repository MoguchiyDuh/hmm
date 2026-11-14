from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from harness import Harness
from hmm_contracts.algo import (
    Area,
    MemberHistory,
    MentorshipRole,
    OnboardingBaseline,
    ScoreWithReason,
)
from hmm_contracts.common import (
    ActivityType,
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    ConfirmationSource,
    ConfirmKind,
    ErrorCode,
    EventFormat,
    FactType,
    FlagStatus,
    FlagSubjectType,
    HelpRequestFormat,
    HelpRequestStatus,
    JoinedVia,
    MemberRole,
    MemberState,
    MentorshipKind,
    MentorshipOutcome,
    MentorshipStatus,
    PlaceStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    RestrictionKind,
    ResultOutcome,
    ReviewContext,
    ReviewSubjectType,
    Severity,
    TeamStatus,
    Trust,
)
from hmm_core import models
from hmm_core.onboarding.baseline import save_onboarding_baseline
from hmm_core.platform.errors import ProblemError
from hmm_core.scores.history import load_member_history
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def event_row(slug: str, starts_at: datetime) -> models.Event:
    return models.Event(
        slug=slug,
        kind="hackathon",
        formation_enabled=True,
        title=slug.title(),
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=1),
        timezone="UTC",
        format="offline",
        language=[],
        themes=["ai"],
        status="published",
        review_reasons=[],
        origin="manual",
        unmapped_areas=[],
    )


def attendance_row(
    event_id: UUID, user_id: UUID, status: AttendanceStatus, now: datetime, **fields: Any
) -> models.EventAttendance:
    return models.EventAttendance(
        event_id=event_id,
        user_id=user_id,
        status=status,
        role=fields.pop("role", AttendanceRole.ATTENDEE),
        proof_links=fields.pop("proof_links", []),
        trust=fields.pop("trust", Trust.SELF),
        status_at=now,
        **fields,
    )


def project_row(name: str, status: ProjectStatus, **fields: Any) -> models.Project:
    return models.Project(
        name=name,
        description_md=f"{name} description",
        goal=f"{name} goal",
        links=fields.pop("links", []),
        visibility=ProjectVisibility.PUBLIC,
        status=status,
        origin=fields.pop("origin", ProjectOrigin.STANDALONE),
        **fields,
    )


def member_row(
    project_id: UUID, user_id: UUID, joined_at: datetime, **fields: Any
) -> models.ProjectMember:
    return models.ProjectMember(
        project_id=project_id,
        user_id=user_id,
        role=fields.pop("role", MemberRole.MEMBER),
        state=fields.pop("state", MemberState.CONFIRMED),
        trust=fields.pop("trust", Trust.PEER),
        joined_via=fields.pop("joined_via", JoinedVia.INVITE),
        is_team_row=fields.pop("is_team_row", False),
        joined_at=joined_at,
        **fields,
    )


def flag_row(
    user_id: UUID,
    subject_type: FlagSubjectType,
    subject_id: UUID,
    status: FlagStatus,
    **fields: Any,
) -> models.Flag:
    return models.Flag(
        user_id=user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        related_user_ids=fields.pop("related_user_ids", []),
        reason=fields.pop("reason", "Suspicious"),
        severity=Severity.MEDIUM,
        status=status,
        algo_version="fake-1",
        **fields,
    )


def review_row(
    reviewer_id: UUID, reviewee_id: UUID, context: ReviewContext, rating: int | None, **fields: Any
) -> models.Review:
    return models.Review(
        subject_type=ReviewSubjectType.PROJECT,
        subject_id=fields.pop("subject_id"),
        reviewer_id=reviewer_id,
        reviewee_id=reviewee_id,
        context=context,
        rating=rating,
        comment=fields.pop("comment", "Solid"),
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
        kind=fields.pop("kind", MentorshipKind.MENTORING),
        mentor_score_at_start=Decimal("8.00"),
        learner_score_at_start=Decimal("3.00"),
        status=fields.pop("status", MentorshipStatus.ACTIVE),
        started_at=started_at,
        mentor_trust=fields.pop("mentor_trust", Trust.PEER),
        learner_trust=fields.pop("learner_trust", Trust.SELF),
        **fields,
    )
    session.add(mentorship)
    await session.flush()
    return mentorship


async def history(h: Harness, user_id: UUID) -> MemberHistory:
    async with h.db() as session:
        return await load_member_history(session, user_id, h.now)


async def test_attendance_fact(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    going = await h.user()
    links = [f"https://proof.test/{index}" for index in range(6)]
    async with h.db() as session:
        event = event_row("attended-event", h.now - timedelta(days=3))
        upcoming = event_row("upcoming-event", h.now + timedelta(days=3))
        session.add_all([event, upcoming])
        await session.flush()
        session.add_all(
            [
                models.EventArea(
                    event_id=event.id,
                    area_id=areas["design"].id,
                    importance=AreaImportance.NICE,
                    headcount=None,
                ),
                models.EventArea(
                    event_id=event.id,
                    area_id=areas["backend"].id,
                    importance=AreaImportance.PRIMARY,
                    headcount=4,
                ),
                attendance_row(
                    event.id,
                    member.id,
                    AttendanceStatus.ATTENDED,
                    h.now,
                    role=AttendanceRole.SPEAKER,
                    proof_links=links,
                    note="Gave a talk",
                    trust=Trust.PEER,
                ),
                attendance_row(event.id, other.id, AttendanceStatus.ATTENDED, h.now),
                attendance_row(event.id, going.id, AttendanceStatus.GOING, h.now),
                attendance_row(upcoming.id, member.id, AttendanceStatus.GOING, h.now),
            ]
        )
    loaded = await history(h, member.id)
    assert len(loaded.facts) == 1
    fact = loaded.facts[0]
    assert fact.type == FactType.ATTENDANCE
    assert fact.role == AttendanceRole.SPEAKER
    assert fact.area == "backend"
    assert fact.trust == Trust.PEER
    assert fact.proof_links == links[:5]
    assert fact.note == "Gave a talk"
    assert fact.started_at == event.starts_at
    assert fact.ended_at == event.ends_at
    assert fact.counterparts == [other.id]
    assert fact.outcome is None
    assert fact.flag_status is None
    assert fact.excluded is False
    assert fact.context.attendance_status == AttendanceStatus.ATTENDED
    assert fact.context.project is None
    snapshot = fact.context.event
    assert snapshot is not None
    assert snapshot.id == event.id
    assert snapshot.format == EventFormat.OFFLINE
    assert snapshot.themes == ["ai"]
    assert [(row.area, row.importance, row.headcount) for row in snapshot.areas] == [
        ("backend", AreaImportance.PRIMARY, 4),
        ("design", AreaImportance.NICE, None),
    ]


async def test_confirmation_metadata(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    confirmer = await h.user()
    withdrawn_confirmer = await h.user()
    admin = await h.admin()
    async with h.db() as session:
        event = event_row("confirmed-event", h.now - timedelta(days=2))
        session.add(event)
        await session.flush()
        attendance = attendance_row(event.id, member.id, AttendanceStatus.ATTENDED, h.now)
        session.add(attendance)
        await session.flush()
        session.add_all(
            [
                models.MemberMetric(
                    user_id=confirmer.id,
                    xp=Decimal("30"),
                    activity=Decimal("1"),
                    algo_version="fake-1",
                    input_hash="input",
                    computed_at=h.now,
                ),
                models.XpAdjustment(
                    user_id=confirmer.id, amount=5, reason="bonus", admin_id=admin.id
                ),
                models.MemberRestriction(
                    user_id=confirmer.id,
                    kind=RestrictionKind.CONFIRMING,
                    until=h.now + timedelta(days=1),
                    reason="Ring",
                    created_by=admin.id,
                ),
                models.MemberRestriction(
                    user_id=confirmer.id,
                    kind=RestrictionKind.MENTORING,
                    until=h.now - timedelta(days=1),
                    reason="Expired",
                    created_by=admin.id,
                ),
                models.MemberRestriction(
                    user_id=confirmer.id,
                    kind=RestrictionKind.CREATING_EVENTS,
                    reason="Lifted",
                    created_by=admin.id,
                    lifted_at=h.now,
                ),
                models.Confirmation(
                    fact_type=FactType.ATTENDANCE,
                    fact_id=attendance.id,
                    subject_user_id=member.id,
                    confirmer_id=withdrawn_confirmer.id,
                    source=ConfirmationSource.PEER,
                    ignored=False,
                    withdrawn_at=h.now,
                    created_at=h.now - timedelta(hours=3),
                ),
                models.Confirmation(
                    fact_type=FactType.ATTENDANCE,
                    fact_id=attendance.id,
                    subject_user_id=member.id,
                    confirmer_id=confirmer.id,
                    source=ConfirmationSource.PEER,
                    ignored=False,
                    created_at=h.now - timedelta(hours=2),
                ),
                models.Confirmation(
                    fact_type=FactType.ATTENDANCE,
                    fact_id=attendance.id,
                    subject_user_id=member.id,
                    confirmer_id=None,
                    source=ConfirmationSource.SYSTEM,
                    ignored=False,
                    created_at=h.now - timedelta(hours=1),
                ),
            ]
        )
    loaded = await history(h, member.id)
    fact = loaded.facts[0]
    assert fact.confirmed_at == h.now - timedelta(hours=2)
    withdrawn, peer, system = fact.confirmations
    assert withdrawn.confirmer_id == withdrawn_confirmer.id
    assert withdrawn.withdrawn_at == h.now
    assert withdrawn.confirmer_xp == 0.0
    assert peer.confirmer_id == confirmer.id
    assert peer.source == ConfirmationSource.PEER
    assert peer.confirmer_account_age == h.now - confirmer.created_at
    assert peer.confirmer_xp == 35.0
    assert peer.confirmer_restrictions == [RestrictionKind.CONFIRMING]
    assert peer.ignored is False
    assert peer.flag_status is None
    assert system.source == ConfirmationSource.SYSTEM
    assert system.confirmer_id is None
    assert system.confirmer_account_age is None
    assert system.confirmer_xp is None
    assert system.confirmer_restrictions == []


async def test_project_facts(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    proposed = await h.user()
    async with h.db() as session:
        standalone = project_row(
            "Standalone",
            ProjectStatus.SHIPPED,
            links=["https://repo.test/standalone"],
            recruiting={"areas": ["frontend"], "note": "Need UI"},
        )
        event = event_row("team-event", h.now - timedelta(days=5))
        session.add_all([standalone, event])
        await session.flush()
        team = project_row(
            "Team",
            ProjectStatus.ACTIVE,
            event_id=event.id,
            team_status=TeamStatus.FINISHED,
            origin=ProjectOrigin.FORMATION,
        )
        session.add(team)
        await session.flush()
        release = models.ProjectRelease(
            project_id=standalone.id,
            title="v1",
            notes_md="First",
            repo_url="https://repo.test/v1",
            created_by=member.id,
        )
        session.add_all(
            [
                release,
                models.ProjectArea(project_id=standalone.id, area_id=areas["frontend"].id),
                models.ProjectArea(project_id=standalone.id, area_id=areas["backend"].id),
                member_row(
                    standalone.id,
                    member.id,
                    h.now - timedelta(days=20),
                    area_id=areas["backend"].id,
                    confirmed_at=h.now - timedelta(days=19),
                ),
                member_row(
                    standalone.id, other.id, h.now - timedelta(days=20), role=MemberRole.LEAD
                ),
                member_row(
                    standalone.id,
                    proposed.id,
                    h.now,
                    state=MemberState.PROPOSED,
                    confirm_by=h.now + timedelta(days=1),
                    confirm_kind=ConfirmKind.PLACEMENT,
                ),
                member_row(
                    team.id,
                    member.id,
                    h.now - timedelta(days=6),
                    event_id=event.id,
                    role=MemberRole.LEAD,
                    is_team_row=True,
                    joined_via=JoinedVia.FORMATION,
                ),
                models.ProjectResult(
                    project_id=team.id,
                    outcome=ResultOutcome.FINISHED,
                    place=1,
                    award="Best",
                    place_status=PlaceStatus.VERIFIED,
                    repo_url="https://repo.test/team",
                    demo_url="https://demo.test/team",
                    presentation_url=None,
                    retrospective_md=None,
                    member_areas={str(member.id): "data"},
                    participants={str(member.id): False},
                    auto_closed=False,
                    submitted_at=h.now - timedelta(days=4),
                ),
                models.ProjectUpdate(
                    project_id=standalone.id,
                    author_id=member.id,
                    body_md="First update",
                    links=["https://update.test/1"],
                    created_at=h.now - timedelta(days=10),
                ),
                models.ProjectUpdate(
                    project_id=standalone.id,
                    author_id=member.id,
                    body_md="Removed update",
                    links=[],
                    created_at=h.now - timedelta(days=9),
                    deleted_at=h.now - timedelta(days=8),
                ),
                models.ProjectUpdate(
                    project_id=standalone.id,
                    author_id=other.id,
                    body_md="Lead update",
                    links=[],
                ),
                models.ProjectMilestone(
                    project_id=standalone.id,
                    title="MVP",
                    description="Ship MVP",
                    due_on=date(2026, 11, 1),
                    position=1,
                    done_by=member.id,
                    done_at=h.now - timedelta(days=7),
                ),
                models.ProjectMilestone(
                    project_id=standalone.id,
                    title="Docs",
                    position=2,
                    done_by=other.id,
                    done_at=h.now - timedelta(days=6),
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                models.ReleaseContribution(
                    release_id=release.id,
                    user_id=member.id,
                    contributed=True,
                    area_id=areas["backend"].id,
                ),
                models.ReleaseContribution(
                    release_id=release.id, user_id=other.id, contributed=False, area_id=None
                ),
            ]
        )
    loaded = await history(h, member.id)
    facts = {fact.context.project.id: fact for fact in loaded.facts if fact.context.project}
    assert len(loaded.facts) == 2
    solo = facts[standalone.id]
    assert solo.type == FactType.PROJECT_MEMBER
    assert solo.role == MemberRole.MEMBER
    assert solo.area == "backend"
    assert solo.trust == Trust.PEER
    assert solo.outcome == ProjectStatus.SHIPPED
    assert solo.counterparts == [other.id]
    assert solo.confirmed_at == h.now - timedelta(days=19)
    assert solo.started_at == h.now - timedelta(days=20)
    assert solo.ended_at is None
    assert solo.context.event is None
    assert solo.context.result is None
    project = solo.context.project
    assert project is not None
    assert project.areas == ["backend", "frontend"]
    assert project.recruiting is not None
    assert project.recruiting.areas == ["frontend"]
    assert project.status == ProjectStatus.SHIPPED
    assert project.team_status is None
    assert project.links == ["https://repo.test/standalone"]
    assert [row.body_md for row in solo.effort.updates] == ["First update", "Removed update"]
    assert solo.effort.updates[0].links == ["https://update.test/1"]
    assert solo.effort.updates[1].deleted_at == h.now - timedelta(days=8)
    assert [row.title for row in solo.effort.milestones] == ["MVP"]
    assert solo.effort.milestones[0].due_on == date(2026, 11, 1)
    assert solo.effort.checkins == []
    assert len(solo.context.releases) == 1
    assert solo.context.releases[0].id == release.id
    assert solo.context.releases[0].repo_url == "https://repo.test/v1"
    assert [
        (row.user_id, row.contributed, row.area) for row in solo.context.releases[0].contributions
    ] == sorted([(member.id, True, "backend"), (other.id, False, None)])
    hackathon = facts[team.id]
    assert hackathon.role == MemberRole.LEAD
    assert hackathon.area == "data"
    assert hackathon.outcome == ResultOutcome.FINISHED
    assert hackathon.confirmed_at is None
    assert hackathon.counterparts == []
    assert hackathon.context.event is not None
    assert hackathon.context.event.id == event.id
    assert hackathon.context.project is not None
    assert hackathon.context.project.team_status == TeamStatus.FINISHED
    result = hackathon.context.result
    assert result is not None
    assert result.outcome == ResultOutcome.FINISHED
    assert result.place == 1
    assert result.award == "Best"
    assert result.place_status == PlaceStatus.VERIFIED
    assert result.participants == {member.id: False}
    assert result.member_areas == {member.id: "data"}
    assert result.repo_url == "https://repo.test/team"
    assert result.demo_url == "https://demo.test/team"
    assert result.presentation_url is None
    assert result.retrospective_md == ""
    assert result.auto_closed is False
    assert result.submitted_at == h.now - timedelta(days=4)


async def test_mentorship_facts(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    mentor = await h.user()
    learner = await h.user()
    peer = await h.user()
    started_at = h.now - timedelta(days=10)
    closed_at = h.now - timedelta(days=2)
    async with h.db() as session:
        await add_mentorship(
            session,
            areas["data"].id,
            learner.id,
            mentor.id,
            h.now - timedelta(days=40),
            status=MentorshipStatus.CLOSED,
            closed_at=h.now - timedelta(days=30),
        )
        mentorship = await add_mentorship(
            session,
            areas["backend"].id,
            mentor.id,
            learner.id,
            started_at,
            status=MentorshipStatus.CLOSED,
            closed_at=closed_at,
            outcome=MentorshipOutcome.REACHED,
            summary="Went well",
            endorsement="Great mentor",
            mentor_acknowledged_at=closed_at,
        )
        study = await add_mentorship(
            session,
            areas["qa"].id,
            mentor.id,
            peer.id,
            h.now - timedelta(days=1),
            kind=MentorshipKind.STUDY_PAIR,
        )
        session.add_all(
            [
                models.MentorshipCheckin(
                    mentorship_id=mentorship.id,
                    author_id=mentor.id,
                    note="Second",
                    occurred_on=date(2026, 11, 8),
                    minutes=45,
                ),
                models.MentorshipCheckin(
                    mentorship_id=mentorship.id,
                    author_id=mentor.id,
                    note="First",
                    occurred_on=date(2026, 11, 5),
                    minutes=30,
                ),
                models.MentorshipCheckin(
                    mentorship_id=mentorship.id,
                    author_id=learner.id,
                    note="Learner",
                    occurred_on=date(2026, 11, 6),
                    minutes=None,
                ),
                models.AreaScore(
                    user_id=learner.id,
                    area_id=areas["backend"].id,
                    calculated=Decimal("4.00"),
                    calc_reason="quiz",
                    override=Decimal("5.50"),
                    override_evidence="github_review",
                    override_reason="Review",
                ),
            ]
        )
    mentor_history = await history(h, mentor.id)
    learner_history = await history(h, learner.id)
    peer_history = await history(h, peer.id)
    mentor_fact = next(fact for fact in mentor_history.facts if fact.id == mentorship.id)
    learner_fact = next(fact for fact in learner_history.facts if fact.id == mentorship.id)
    assert mentor_fact.type == learner_fact.type == FactType.MENTORSHIP_SIDE
    assert mentor_fact.role == MentorshipRole.MENTOR
    assert learner_fact.role == MentorshipRole.MENTEE
    assert mentor_fact.trust == Trust.PEER
    assert learner_fact.trust == Trust.SELF
    assert mentor_fact.counterparts == [learner.id]
    assert learner_fact.counterparts == [mentor.id]
    assert mentor_fact.area == "backend"
    assert mentor_fact.started_at == started_at
    assert mentor_fact.ended_at == closed_at
    assert mentor_fact.outcome == MentorshipOutcome.REACHED
    assert [row.note for row in mentor_fact.effort.checkins] == ["First", "Second"]
    assert mentor_fact.effort.minutes == 75
    assert [row.note for row in learner_fact.effort.checkins] == ["Learner"]
    assert learner_fact.effort.minutes == 0
    context = mentor_fact.context.mentorship
    assert context is not None
    assert context == learner_fact.context.mentorship
    assert context.kind == MentorshipKind.MENTORING
    assert context.status == MentorshipStatus.CLOSED
    assert context.area == "backend"
    assert context.mentor_score_at_start == 8.0
    assert context.learner_score_at_start == 3.0
    assert context.learner_score_now == 5.5
    assert context.duration == closed_at - started_at
    assert context.mentor_checkin_count == 2
    assert context.learner_checkin_count == 1
    assert context.earlier_pair_mentorships == 1
    assert context.summary == "Went well"
    assert context.endorsement == "Great mentor"
    assert context.mentor_acknowledged_at == closed_at
    assert mentor_fact.context.event is None
    assert mentor_fact.context.releases == []
    earlier = [fact for fact in mentor_history.facts if fact.area == "data"]
    assert [fact.role for fact in earlier] == [MentorshipRole.MENTEE]
    assert earlier[0].context.mentorship is not None
    assert earlier[0].context.mentorship.earlier_pair_mentorships == 0
    assert earlier[0].context.mentorship.learner_score_now == 0.0
    study_mentor = next(fact for fact in mentor_history.facts if fact.id == study.id)
    study_peer = next(fact for fact in peer_history.facts if fact.id == study.id)
    assert study_mentor.role == study_peer.role == MentorshipRole.PEER
    assert study_mentor.context.mentorship is not None
    assert study_mentor.context.mentorship.duration == timedelta(days=1)


async def test_solo_entries(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    today = h.now.date()
    async with h.db() as session:
        kept = models.ActivityEntry(
            user_id=member.id,
            type=ActivityType.COURSE,
            title="Course",
            description_md="Finished a course",
            links=["https://course.test"],
            area_id=areas["ml_ai"].id,
            occurred_on=today,
            created_at=h.now - timedelta(hours=2),
        )
        deleted = models.ActivityEntry(
            user_id=member.id,
            type=ActivityType.TALK,
            title="Talk",
            description_md="Gave a talk",
            links=[],
            occurred_on=today - timedelta(days=1),
            created_at=h.now - timedelta(hours=1),
            deleted_at=h.now,
        )
        session.add_all([kept, deleted])
    loaded = await history(h, member.id)
    assert [entry.id for entry in loaded.activity_entries] == [kept.id, deleted.id]
    first, second = loaded.activity_entries
    assert first.type == ActivityType.COURSE
    assert first.area == "ml_ai"
    assert first.links == ["https://course.test"]
    assert first.deleted_at is None
    assert first.excluded is False
    assert second.area is None
    assert second.deleted_at == h.now
    assert second.excluded is True
    facts = {fact.id: fact for fact in loaded.facts}
    assert facts[kept.id].type == FactType.ACTIVITY_ENTRY
    assert facts[kept.id].area == "ml_ai"
    assert facts[kept.id].trust == Trust.SELF
    assert facts[kept.id].role is None
    assert facts[kept.id].excluded is False
    assert facts[kept.id].started_at == datetime.combine(today, time(), tzinfo=UTC)
    assert facts[deleted.id].excluded is True
    async with h.db() as session:
        ctx = await h.runtime.algo_context(session)
        area_list = [
            Area.model_validate(area, from_attributes=True)
            for area in await session.scalars(select(models.Area).order_by(models.Area.sort))
        ]
    metrics = await h.algorithm.compute_member(ctx, loaded, area_list)
    awards = {award.fact_id: award for award in metrics.fact_awards}
    assert awards[kept.id].activity == 1.0
    assert awards[deleted.id].activity == 0.0


async def test_flags_and_pardons(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    other = await h.user()
    async with h.db() as session:
        event = event_row("flagged-event", h.now - timedelta(days=2))
        project = project_row("Flagged", ProjectStatus.ACTIVE)
        session.add_all([event, project])
        await session.flush()
        attendance = attendance_row(event.id, member.id, AttendanceStatus.ATTENDED, h.now)
        membership = member_row(project.id, member.id, h.now - timedelta(days=3))
        session.add_all([attendance, membership])
        await session.flush()
        session.add_all(
            [
                flag_row(
                    member.id,
                    FlagSubjectType.ATTENDANCE,
                    attendance.id,
                    FlagStatus.PARDONED,
                    created_at=h.now - timedelta(days=1),
                    reason="Old",
                ),
                flag_row(member.id, FlagSubjectType.ATTENDANCE, attendance.id, FlagStatus.TO_CHECK),
                flag_row(
                    member.id,
                    FlagSubjectType.PROJECT_MEMBER,
                    membership.id,
                    FlagStatus.PUNISHED,
                    penalty={"exclude": True, "xp_penalty": 0},
                ),
                flag_row(
                    other.id,
                    FlagSubjectType.RING,
                    other.id,
                    FlagStatus.PARDONED,
                    related_user_ids=[member.id],
                    reason="r" * 250,
                ),
                flag_row(
                    other.id,
                    FlagSubjectType.RING,
                    event.id,
                    FlagStatus.PARDONED,
                    related_user_ids=[other.id],
                ),
            ]
        )
    loaded = await history(h, member.id)
    facts = {fact.type: fact for fact in loaded.facts}
    assert facts[FactType.ATTENDANCE].flag_status == FlagStatus.TO_CHECK
    assert facts[FactType.ATTENDANCE].excluded is False
    assert facts[FactType.PROJECT_MEMBER].flag_status == FlagStatus.PUNISHED
    assert facts[FactType.PROJECT_MEMBER].excluded is True
    ring = next(row for row in loaded.pardoned if row.subject_type == FlagSubjectType.RING)
    assert ring.subject_id == other.id
    assert len(ring.reason) == 200
    assert [(row.subject_type, row.subject_id) for row in loaded.pardoned] == [
        (FlagSubjectType.ATTENDANCE, attendance.id),
        (FlagSubjectType.RING, other.id),
    ]


async def test_reviews_and_adjustments(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    reviewer = await h.user()
    admin = await h.admin()
    async with h.db() as session:
        project = project_row("Reviewed", ProjectStatus.SHIPPED)
        session.add(project)
        await session.flush()
        flagged = review_row(
            reviewer.id,
            member.id,
            ReviewContext.MENTOR,
            7,
            subject_id=project.id,
            revealed_at=h.now,
            created_at=h.now - timedelta(hours=1),
        )
        rows = [
            review_row(
                reviewer.id,
                member.id,
                ReviewContext.MEMBER,
                8,
                subject_id=member.id,
                revealed_at=h.now,
                created_at=h.now - timedelta(hours=2),
            ),
            flagged,
            review_row(reviewer.id, member.id, ReviewContext.LEAD, 9, subject_id=reviewer.id),
            review_row(
                reviewer.id,
                member.id,
                ReviewContext.HOST,
                2,
                subject_id=admin.id,
                revealed_at=h.now,
                removed_at=h.now,
            ),
            models.XpAdjustment(
                user_id=member.id, amount=Decimal("7.5"), reason="a", admin_id=admin.id
            ),
            models.XpAdjustment(user_id=member.id, amount=-3, reason="b", admin_id=admin.id),
        ]
        session.add_all(rows)
        await session.flush()
        session.add(flag_row(reviewer.id, FlagSubjectType.REVIEW, flagged.id, FlagStatus.TO_CHECK))
    loaded = await history(h, member.id)
    assert [(row.context, row.rating) for row in loaded.reviews_received] == [
        (ReviewContext.MEMBER, 8),
        (ReviewContext.MENTOR, 7),
    ]
    first, second = loaded.reviews_received
    assert first.flag_status is None
    assert second.flag_status == FlagStatus.TO_CHECK
    assert second.id == flagged.id
    assert second.subject_type == ReviewSubjectType.PROJECT
    assert second.subject_id == project.id
    assert second.reviewer_id == reviewer.id
    assert second.reviewee_id == member.id
    assert second.comment == "Solid"
    assert loaded.xp_adjustments_total == 4.5
    assert loaded.xp == 4.5


async def test_onboarding_baseline(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    fresh = await h.user()
    baseline = OnboardingBaseline(
        submitted_at=h.now,
        area_scores={"backend": ScoreWithReason(value=6.5, reason="quiz")},
        discrepancies=["CV claims more"],
    )
    async with h.db() as session:
        await save_onboarding_baseline(session, member.id, baseline)
    loaded = await history(h, member.id)
    assert loaded.onboarding is not None
    assert loaded.onboarding.area_scores["backend"].value == 6.5
    assert loaded.onboarding.discrepancies == ["CV claims more"]
    assert (await history(h, fresh.id)).onboarding is None


async def test_ordering_and_determinism(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    async with h.db() as session:
        event = event_row("ordered-event", h.now - timedelta(days=4))
        project = project_row("Ordered", ProjectStatus.ACTIVE)
        session.add_all([event, project])
        await session.flush()
        session.add_all(
            [
                attendance_row(event.id, member.id, AttendanceStatus.ATTENDED, h.now),
                member_row(project.id, member.id, h.now - timedelta(days=4)),
                models.ActivityEntry(
                    user_id=member.id,
                    type=ActivityType.OSS,
                    title="Patch",
                    description_md="",
                    links=[],
                    occurred_on=(h.now - timedelta(days=6)).date(),
                ),
            ]
        )
        await add_mentorship(
            session, areas["backend"].id, member.id, other.id, h.now - timedelta(days=4)
        )
        await add_mentorship(
            session, areas["data"].id, other.id, member.id, h.now - timedelta(days=1)
        )
    first = await history(h, member.id)
    second = await history(h, member.id)
    assert len(first.facts) == 5
    keys = [(fact.started_at, fact.type.value, str(fact.id)) for fact in first.facts]
    assert keys == sorted(keys)
    assert first.facts[0].type == FactType.ACTIVITY_ENTRY
    assert [fact.type for fact in first.facts[1:4]] == [
        FactType.ATTENDANCE,
        FactType.MENTORSHIP_SIDE,
        FactType.PROJECT_MEMBER,
    ]
    assert first.model_dump() == second.model_dump()


async def test_missing_member(harness: Harness) -> None:
    h = harness
    pending = await h.user(onboarded=False)
    async with h.db() as session:
        with pytest.raises(ProblemError) as error:
            await load_member_history(session, pending.id, h.now)
    assert error.value.code == ErrorCode.NOT_FOUND
