from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import EffectiveScore, MentorOffer
from hmm_contracts.common import (
    ConfirmKind,
    CvTextStatus,
    ErrorCode,
    GithubSnapshotStatus,
    HelpRequestFormat,
    HelpRequestStatus,
    JoinedVia,
    MemberReviewStatus,
    MemberRole,
    MemberState,
    MentorshipKind,
    MentorshipStatus,
    OnboardingStatus,
    OverrideEvidence,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
    Trust,
)
from hmm_core import models
from hmm_core.platform.errors import ProblemError
from hmm_core.scores.snapshot import build_member_snapshot, build_member_snapshots


@pytest.mark.parametrize(
    "github_status,cv_status",
    [
        (GithubSnapshotStatus.OK, CvTextStatus.OK),
        (GithubSnapshotStatus.ERROR, CvTextStatus.FAILED),
    ],
)
async def test_snapshot_profile_inputs(
    harness: Harness, github_status: GithubSnapshotStatus, cv_status: CvTextStatus
) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user(display_name="Octo", about_md="About Octo")
    account_created_at = h.now - timedelta(days=100)
    pushed_at = h.now - timedelta(days=1)
    async with h.db() as session:
        session.add_all(
            [
                models.CvFile(
                    user_id=member.id,
                    object_key="cv/octo.pdf",
                    filename="octo.pdf",
                    mime="application/pdf",
                    size_bytes=100,
                    uploaded_at=h.now,
                    text="cv text",
                    text_status=cv_status,
                ),
                models.GithubSnapshot(
                    user_id=member.id,
                    fetched_at=h.now,
                    status=github_status,
                    profile={
                        "login": "octo",
                        "account_created_at": account_created_at.isoformat(),
                    },
                    languages={"Python": 300, "Rust": 100},
                    stats={
                        "followers": 5,
                        "public_repos": 7,
                        "contributions_last_year": 42,
                        "stars_total": 9,
                    },
                    repos=[
                        {
                            "name": "hmm",
                            "description": "A project",
                            "topics": ["python", "rust"],
                            "language": "Python",
                            "stars": 9,
                            "pushed_at": pushed_at.isoformat(),
                        }
                    ],
                ),
            ]
        )
    async with h.db() as session:
        snapshot = await build_member_snapshot(session, member.id, h.now)
        repeated = await build_member_snapshot(session, member.id, h.now)
    assert snapshot.user_id == member.id
    assert snapshot.display_name == "Octo"
    assert snapshot.about_md == "About Octo"
    assert snapshot.cv_text == "cv text"
    assert snapshot.joined_at == member.created_at
    assert snapshot.mentor_notified_at == []
    assert snapshot.github is not None
    assert snapshot.github.login == "octo"
    assert snapshot.github.account_created_at == account_created_at
    assert snapshot.github.languages == {"Python": 75.0, "Rust": 25.0}
    assert snapshot.github.followers == 5
    assert snapshot.github.public_repos == 7
    assert snapshot.github.contributions_last_year == 42
    assert snapshot.github.stars_total == 9
    assert len(snapshot.github.repos) == 1
    repo = snapshot.github.repos[0]
    assert repo.name == "hmm"
    assert repo.description == "A project"
    assert repo.topics == ["python", "rust"]
    assert repo.language == "Python"
    assert repo.stars == 9
    assert repo.pushed_at == pushed_at
    assert snapshot.model_dump(mode="json") == repeated.model_dump(mode="json")


async def test_snapshot_scores_metrics_offers_and_reviews(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user(mentor_capacity=2)
    reviewer = await h.admin()
    reviewed_at = h.now - timedelta(days=1)
    async with h.db() as session:
        session.add_all(
            [
                models.AreaScore(
                    user_id=member.id,
                    area_id=areas["backend"].id,
                    calculated=Decimal("4.25"),
                    calc_reason="quiz",
                    override=Decimal("7.50"),
                    override_evidence=OverrideEvidence.GITHUB_REVIEW,
                    override_reason="GitHub review",
                ),
                models.AreaScore(
                    user_id=member.id,
                    area_id=areas["frontend"].id,
                    calculated=Decimal("3.00"),
                    calc_reason="quiz",
                ),
                models.AreaScore(
                    user_id=member.id,
                    area_id=areas["qa"].id,
                    calculated=Decimal("8.00"),
                    calc_reason="quiz",
                ),
                models.MemberMetric(
                    user_id=member.id,
                    xp=Decimal("20"),
                    activity=Decimal("3.5"),
                    algo_version="fake-1",
                    input_hash="input",
                    computed_at=h.now,
                ),
                models.XpAdjustment(
                    user_id=member.id, amount=5, reason="bonus", admin_id=reviewer.id
                ),
                models.XpAdjustment(
                    user_id=member.id, amount=-2, reason="correction", admin_id=reviewer.id
                ),
                models.XpAdjustment(
                    user_id=other.id, amount=4, reason="bonus", admin_id=reviewer.id
                ),
                models.MemberReview(
                    user_id=member.id,
                    status=MemberReviewStatus.REVIEWED,
                    opened_at=h.now - timedelta(days=2),
                    reviewed_at=reviewed_at,
                    reviewed_by=reviewer.id,
                    discrepancies=[],
                ),
                models.MemberReview(
                    user_id=other.id,
                    status=MemberReviewStatus.PENDING,
                    opened_at=h.now,
                    reviewed_at=reviewed_at,
                    discrepancies=[],
                ),
                models.MentorOffer(
                    user_id=member.id,
                    area_id=areas["data"].id,
                    topics="Data engineering",
                    active=True,
                ),
                models.MentorOffer(
                    user_id=member.id,
                    area_id=areas["backend"].id,
                    topics="APIs",
                    active=True,
                ),
                models.MentorOffer(
                    user_id=member.id,
                    area_id=areas["frontend"].id,
                    topics="Interfaces",
                    active=False,
                ),
                models.MentorOffer(
                    user_id=member.id,
                    area_id=areas["qa"].id,
                    topics="Testing",
                    active=True,
                ),
            ]
        )
        qa = await session.get(models.Area, areas["qa"].id)
        data = await session.get(models.Area, areas["data"].id)
        assert qa is not None
        assert data is not None
        qa.archived_at = h.now
        data.sort = areas["backend"].sort
    async with h.db() as session:
        snapshots = await build_member_snapshots(session, [member.id, other.id], h.now)
    snapshot = snapshots[member.id]
    assert snapshot.area_scores == {
        "backend": EffectiveScore(
            calculated=4.25,
            override=7.5,
            override_evidence=OverrideEvidence.GITHUB_REVIEW,
            effective=7.5,
        ),
        "frontend": EffectiveScore(
            calculated=3.0, override=None, override_evidence=None, effective=3.0
        ),
    }
    assert snapshot.xp == 23.0
    assert snapshot.activity == 3.5
    assert snapshot.scores_reviewed_at == reviewed_at
    assert snapshot.mentor_offers == [
        MentorOffer(area="backend", topics="APIs", active=True),
        MentorOffer(area="data", topics="Data engineering", active=True),
        MentorOffer(area="frontend", topics="Interfaces", active=False),
    ]
    assert snapshot.mentor_capacity == 3
    assert snapshots[other.id].xp == 4.0
    assert snapshots[other.id].activity == 0.0
    assert snapshots[other.id].mentor_capacity == 2
    assert snapshots[other.id].scores_reviewed_at is None
    assert snapshots[other.id].area_scores == {}
    assert snapshots[other.id].mentor_offers == []


async def test_snapshot_project_counts(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user()
    async with h.db() as session:
        event = models.Event(
            slug="finished-hackathon",
            kind="hackathon",
            formation_enabled=True,
            title="Finished hackathon",
            starts_at=h.now - timedelta(days=2),
            ends_at=h.now - timedelta(days=1),
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
        team = models.Project(
            event_id=event.id,
            name="Finished team",
            description_md="",
            goal="Ship",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.SHIPPED,
            team_status=TeamStatus.FINISHED,
            origin=ProjectOrigin.FORMATION,
        )
        shipped = models.Project(
            name="Shipped project",
            description_md="",
            goal="Ship",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.SHIPPED,
            origin=ProjectOrigin.STANDALONE,
        )
        proposed = models.Project(
            name="Proposed project",
            description_md="",
            goal="Ship",
            links=[],
            visibility=ProjectVisibility.PUBLIC,
            status=ProjectStatus.SHIPPED,
            origin=ProjectOrigin.STANDALONE,
        )
        session.add_all([team, shipped, proposed])
        await session.flush()
        session.add_all(
            [
                models.ProjectMember(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=member.id,
                    role=MemberRole.LEAD,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.FORMATION,
                    is_team_row=True,
                    joined_at=h.now - timedelta(days=3),
                    left_at=h.now,
                ),
                models.ProjectMember(
                    project_id=team.id,
                    event_id=event.id,
                    user_id=member.id,
                    role=MemberRole.LEAD,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.FORMATION,
                    is_team_row=True,
                    joined_at=h.now - timedelta(days=4),
                    left_at=h.now - timedelta(days=3),
                ),
                models.ProjectMember(
                    project_id=shipped.id,
                    user_id=member.id,
                    role=MemberRole.MEMBER,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.INVITE,
                    is_team_row=False,
                    joined_at=h.now - timedelta(days=2),
                    left_at=h.now,
                ),
                models.ProjectMember(
                    project_id=shipped.id,
                    user_id=member.id,
                    role=MemberRole.MEMBER,
                    state=MemberState.CONFIRMED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.INVITE,
                    is_team_row=False,
                    joined_at=h.now,
                ),
                models.ProjectMember(
                    project_id=proposed.id,
                    user_id=member.id,
                    role=MemberRole.LEAD,
                    state=MemberState.PROPOSED,
                    trust=Trust.SELF,
                    joined_via=JoinedVia.INVITE,
                    is_team_row=False,
                    joined_at=h.now,
                    confirm_by=h.now + timedelta(days=1),
                    confirm_kind=ConfirmKind.PLACEMENT,
                ),
            ]
        )
    async with h.db() as session:
        snapshot = await build_member_snapshot(session, member.id, h.now)
    assert snapshot.hackathons_finished == 1
    assert snapshot.times_led == 1
    assert snapshot.projects_shipped == 1


async def test_snapshot_mentorship_counts(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    other = await h.user()
    cases = [
        (member.id, other.id, "backend", MentorshipStatus.CLOSED),
        (other.id, member.id, "data", MentorshipStatus.CLOSED),
        (member.id, other.id, "qa", MentorshipStatus.ACTIVE),
        (other.id, member.id, "design", MentorshipStatus.ACTIVE),
        (member.id, other.id, "security", MentorshipStatus.STALLED),
        (member.id, other.id, "devops", MentorshipStatus.CANCELLED),
    ]
    async with h.db() as session:
        for mentor_id, learner_id, area_key, status in cases:
            request = models.HelpRequest(
                learner_id=learner_id,
                area_id=areas[area_key].id,
                topic="Learn",
                goal="Practice",
                format=HelpRequestFormat.ONLINE,
                status=HelpRequestStatus.MATCHED,
                expires_at=h.now + timedelta(days=1),
            )
            session.add(request)
            await session.flush()
            session.add(
                models.Mentorship(
                    request_id=request.id,
                    mentor_id=mentor_id,
                    learner_id=learner_id,
                    area_id=areas[area_key].id,
                    kind=MentorshipKind.MENTORING,
                    mentor_score_at_start=Decimal("8.00"),
                    learner_score_at_start=Decimal("3.00"),
                    status=status,
                    started_at=h.now - timedelta(days=2),
                    closed_at=h.now if status == MentorshipStatus.CLOSED else None,
                    mentor_trust=Trust.SELF,
                    learner_trust=Trust.SELF,
                )
            )
    async with h.db() as session:
        snapshots = await build_member_snapshots(session, [member.id, other.id], h.now)
    assert snapshots[member.id].mentorships_completed == 2
    assert snapshots[member.id].mentor_active == 1
    assert snapshots[other.id].mentorships_completed == 2
    assert snapshots[other.id].mentor_active == 1


async def test_snapshot_batch_membership_and_defaults(harness: Harness) -> None:
    h = harness
    first = await h.user()
    second = await h.user(mentor_capacity=0)
    pending = await h.user(onboarded=False)
    missing_id = uuid4()
    async with h.db() as session:
        reopened = await session.get(models.User, second.id)
        assert reopened is not None
        reopened.onboarding_status = OnboardingStatus.REOPENED
        snapshots = await build_member_snapshots(
            session, [second.id, first.id, first.id, missing_id, pending.id], h.now
        )
        assert set(snapshots) == {first.id, second.id}
        assert await build_member_snapshots(session, [], h.now) == {}
        assert await build_member_snapshots(session, [missing_id, pending.id], h.now) == {}
        for user_id in (missing_id, pending.id):
            with pytest.raises(ProblemError) as error:
                await build_member_snapshot(session, user_id, h.now)
            assert error.value.code == ErrorCode.NOT_FOUND
    assert snapshots[first.id].user_id == first.id
    assert snapshots[second.id].user_id == second.id
    assert snapshots[second.id].mentor_capacity == 0
    for snapshot in snapshots.values():
        assert snapshot.cv_text is None
        assert snapshot.github is None
        assert snapshot.area_scores == {}
        assert snapshot.scores_reviewed_at is None
        assert snapshot.xp == 0.0
        assert snapshot.activity == 0.0
        assert snapshot.hackathons_finished == 0
        assert snapshot.times_led == 0
        assert snapshot.projects_shipped == 0
        assert snapshot.mentorships_completed == 0
        assert snapshot.mentor_active == 0
        assert snapshot.mentor_offers == []
        assert snapshot.mentor_notified_at == []
