from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode, MentorshipKind, ReviewContext
from hmm_core import models
from hmm_core.results.schemas import Review
from pydantic import JsonValue
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


@dataclass(frozen=True)
class ReviewSetup:
    member: models.User
    lead: models.User
    outsider: models.User
    project: models.Project
    window: models.ReviewWindow


@pytest.fixture
async def review_setup(harness: Harness) -> ReviewSetup:
    member = await harness.user(login="alice", display_name="Alice")
    lead = await harness.user(login="lead", display_name="Zoe")
    outsider = await harness.user(login="bob", display_name="Alex")
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
        window = models.ReviewWindow(
            subject_type="project",
            subject_id=project.id,
            opens_at=harness.now,
            closes_at=harness.now + timedelta(days=7),
            participants={str(member.id): "member", str(lead.id): "lead"},
        )
        session.add(window)
        await session.flush()
    return ReviewSetup(member, lead, outsider, project, window)


async def create_mentorship_window(
    harness: Harness, kind: MentorshipKind
) -> tuple[models.User, models.User, models.ReviewWindow]:
    mentor = await harness.user(login="mentor")
    learner = await harness.user(login="learner")
    area = next(iter((await harness.seed_areas()).values()))
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
        context = ReviewContext.MENTOR if kind == MentorshipKind.MENTORING else ReviewContext.MEMBER
        window = models.ReviewWindow(
            subject_type="mentorship",
            subject_id=mentorship.id,
            opens_at=harness.now,
            closes_at=harness.now + timedelta(days=7),
            participants={str(mentor.id): context.value, str(learner.id): context.value},
        )
        session.add(window)
        await session.flush()
    return mentor, learner, window


async def test_submit_and_edit_project_review(harness: Harness, review_setup: ReviewSetup) -> None:
    case = review_setup
    async with harness.db() as session:
        session.add(case.project)
        case.project.lead_user_id = case.outsider.id
    path = f"/api/v1/review-windows/{case.window.id}/reviews/{case.lead.id}"
    async with await harness.client(case.member) as client:
        response = await client.put(path, json={"rating": 8, "comment": "Great lead"})
        assert response.status_code == 200, response.text
        body = response.json()
        review_id = body["id"]
        assert body["window_id"] == str(case.window.id)
        assert body["subject_type"] == "project"
        assert body["subject_id"] == str(case.project.id)
        assert body["reviewer"] == {
            "id": str(case.member.id),
            "display_name": case.member.display_name,
            "avatar_url": None,
        }
        assert body["reviewee_id"] == str(case.lead.id)
        assert body["context"] == "lead"
        assert body["rating"] == 8
        assert body["comment"] == "Great lead"
        assert body["revealed_at"] is None
        assert body["removed_at"] is None
        assert body["removed_by"] is None
        assert body["removed_reason"] is None
        assert body["allowed_actions"] == ["edit"]
        created = Review.model_validate(body)
        async with harness.db() as session:
            row = (await session.scalars(select(models.Review))).one()
            assert str(row.id) == review_id
            assert row.context == "lead"
            assert row.window_id == case.window.id
            assert row.subject_type == "project"
            assert row.subject_id == case.project.id
            assert row.reviewer_id == case.member.id
            assert row.revealed_at is None
            assert created.created_at == row.created_at
            assert created.updated_at == row.updated_at
        response = await client.put(path, json={"rating": 9, "comment": " Updated "})
        assert response.status_code == 200, response.text
        edited = Review.model_validate(response.json())
        assert str(edited.id) == review_id
        assert edited.rating == 9
        assert edited.comment == "Updated"
        assert edited.created_at == created.created_at
        assert edited.revealed_at is None
        assert response.json()["allowed_actions"] == ["edit"]
    async with harness.db() as session:
        row = (await session.scalars(select(models.Review))).one()
        assert row.rating == 9
        assert row.comment == "Updated"
        assert row.revealed_at is None
        assert edited.updated_at == row.updated_at
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
    assert await harness.pending_jobs() == []


@pytest.mark.parametrize(
    ("scenario", "status", "code"),
    (
        ("self", 403, ErrorCode.FORBIDDEN),
        ("outside_reviewee", 404, ErrorCode.NOT_FOUND),
        ("outside_actor", 404, ErrorCode.NOT_FOUND),
        ("missing_window", 404, ErrorCode.NOT_FOUND),
    ),
)
async def test_submit_review_participant_boundaries(
    harness: Harness,
    review_setup: ReviewSetup,
    scenario: str,
    status: int,
    code: ErrorCode,
) -> None:
    case = review_setup
    actor = case.outsider if scenario == "outside_actor" else case.member
    reviewee = case.member if scenario == "self" else case.lead
    if scenario == "outside_reviewee":
        reviewee = case.outsider
    window_id = uuid4() if scenario == "missing_window" else case.window.id
    async with await harness.client(actor) as client:
        problem = assert_problem(
            await client.put(
                f"/api/v1/review-windows/{window_id}/reviews/{reviewee.id}",
                json={"rating": 8, "comment": "Helpful"},
            ),
            status,
            code,
        )
        if scenario == "self":
            assert problem.detail == "You cannot review yourself"
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Review)) == 0


@pytest.mark.parametrize("scenario", ("deadline", "revealed", "future"))
async def test_review_window_cutoff_preserves_existing_review(
    harness: Harness, review_setup: ReviewSetup, scenario: str
) -> None:
    case = review_setup
    path = f"/api/v1/review-windows/{case.window.id}/reviews/{case.lead.id}"
    async with await harness.client(case.member) as client:
        response = await client.put(path, json={"rating": 8, "comment": "Great lead"})
        assert response.status_code == 200, response.text
        original = Review.model_validate(response.json())
        if scenario == "deadline":
            harness.advance(timedelta(days=7))
        else:
            async with harness.db() as session:
                session.add(case.window)
                if scenario == "revealed":
                    case.window.revealed_at = harness.now
                else:
                    case.window.opens_at = harness.now + timedelta(days=1)
        assert_problem(
            await client.put(path, json={"rating": 9, "comment": "Changed"}),
            409,
            ErrorCode.REVIEW_WINDOW_CLOSED,
        )
    async with harness.db() as session:
        row = (await session.scalars(select(models.Review))).one()
        assert row.id == original.id
        assert row.rating == original.rating
        assert row.comment == original.comment
        assert row.updated_at == original.updated_at
        assert row.revealed_at is None


async def test_mentoring_ratings_and_endorsements(harness: Harness) -> None:
    mentor, learner, window = await create_mentorship_window(harness, MentorshipKind.MENTORING)
    path = f"/api/v1/review-windows/{window.id}/reviews"
    async with await harness.client(learner) as client:
        response = await client.put(
            f"{path}/{mentor.id}", json={"rating": 7, "comment": "Helpful mentor"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["context"] == "mentor"
        assert response.json()["rating"] == 7
        problem = assert_problem(
            await client.put(f"{path}/{mentor.id}", json={"comment": "Helpful mentor"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert problem.detail == "A rating is required"
    async with await harness.client(mentor) as client:
        problem = assert_problem(
            await client.put(f"{path}/{learner.id}", json={"rating": 6, "comment": "Solid"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert problem.detail == "Endorsements have no rating"
        response = await client.put(f"{path}/{learner.id}", json={"comment": "Steady progress"})
        assert response.status_code == 200, response.text
        assert response.json()["context"] == "mentor"
        assert response.json()["rating"] is None
        assert response.json()["comment"] == "Steady progress"
        assert response.json()["allowed_actions"] == ["edit"]
    async with harness.db() as session:
        rows = list(await session.scalars(select(models.Review)))
        assert len(rows) == 2
        assert {row.reviewee_id: row.rating for row in rows} == {mentor.id: 7, learner.id: None}
        assert all(row.context == "mentor" and row.revealed_at is None for row in rows)


async def test_study_pair_both_directions_require_ratings(harness: Harness) -> None:
    mentor, learner, window = await create_mentorship_window(harness, MentorshipKind.STUDY_PAIR)
    for actor, reviewee in ((mentor, learner), (learner, mentor)):
        async with await harness.client(actor) as client:
            path = f"/api/v1/review-windows/{window.id}/reviews/{reviewee.id}"
            assert_problem(
                await client.put(path, json={"comment": "Helpful partner"}),
                422,
                ErrorCode.VALIDATION_ERROR,
            )
            response = await client.put(path, json={"rating": 8, "comment": "Helpful partner"})
            assert response.status_code == 200, response.text
            assert response.json()["context"] == "member"
            assert response.json()["rating"] == 8
    async with harness.db() as session:
        rows = list(await session.scalars(select(models.Review)))
        assert len(rows) == 2
        assert all(row.context == "member" and row.rating == 8 for row in rows)


@pytest.mark.parametrize(
    ("body", "detail"),
    (
        ({"comment": "Helpful"}, "A rating is required"),
        ({"rating": None, "comment": "Helpful"}, "A rating is required"),
        ({"rating": 8, "comment": "   "}, "Comment must not be empty"),
    ),
)
async def test_project_review_requires_rating_and_nonempty_comment(
    harness: Harness,
    review_setup: ReviewSetup,
    body: dict[str, int | str | None],
    detail: str,
) -> None:
    case = review_setup
    async with await harness.client(case.member) as client:
        problem = assert_problem(
            await client.put(
                f"/api/v1/review-windows/{case.window.id}/reviews/{case.lead.id}", json=body
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert problem.detail == detail
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Review)) == 0


async def test_removed_review_cannot_be_edited(harness: Harness, review_setup: ReviewSetup) -> None:
    case = review_setup
    async with harness.db() as session:
        session.add(
            models.Review(
                window_id=case.window.id,
                subject_type="project",
                subject_id=case.project.id,
                reviewer_id=case.member.id,
                reviewee_id=case.lead.id,
                context="lead",
                rating=8,
                comment="Original",
                removed_at=harness.now,
                removed_by=case.lead.id,
                removed_reason="Removed",
            )
        )
    async with await harness.client(case.member) as client:
        problem = assert_problem(
            await client.put(
                f"/api/v1/review-windows/{case.window.id}/reviews/{case.lead.id}",
                json={"rating": 9, "comment": "Changed"},
            ),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert problem.detail == "Review was removed"
    async with harness.db() as session:
        row = (await session.scalars(select(models.Review))).one()
        assert row.rating == 8
        assert row.comment == "Original"
        assert row.removed_at == harness.now


async def test_pending_windows_are_visible_ordered_and_include_own_submissions(
    harness: Harness, review_setup: ReviewSetup
) -> None:
    case = review_setup
    participants: dict[str, JsonValue] = {
        str(case.member.id): "member",
        str(case.lead.id): "lead",
        str(case.outsider.id): "member",
    }
    async with harness.db() as session:
        session.add(case.window)
        case.window.participants = participants
        earlier = models.ReviewWindow(
            subject_type="project",
            subject_id=case.project.id,
            opens_at=harness.now,
            closes_at=harness.now + timedelta(days=1),
            participants=participants,
        )
        session.add_all(
            [
                earlier,
                models.ReviewWindow(
                    subject_type="project",
                    subject_id=case.project.id,
                    opens_at=harness.now,
                    closes_at=harness.now + timedelta(days=2),
                    participants={str(case.lead.id): "lead", str(case.outsider.id): "member"},
                ),
                models.ReviewWindow(
                    subject_type="project",
                    subject_id=case.project.id,
                    opens_at=harness.now - timedelta(days=7),
                    closes_at=harness.now,
                    participants=participants,
                ),
                models.ReviewWindow(
                    subject_type="project",
                    subject_id=case.project.id,
                    opens_at=harness.now + timedelta(seconds=1),
                    closes_at=harness.now + timedelta(days=7),
                    participants=participants,
                ),
                models.ReviewWindow(
                    subject_type="project",
                    subject_id=case.project.id,
                    opens_at=harness.now,
                    closes_at=harness.now + timedelta(days=7),
                    revealed_at=harness.now,
                    participants=participants,
                ),
            ]
        )
        own_review = models.Review(
            window_id=case.window.id,
            subject_type="project",
            subject_id=case.project.id,
            reviewer_id=case.member.id,
            reviewee_id=case.lead.id,
            context="lead",
            rating=8,
            comment="Great lead",
        )
        session.add_all(
            [
                own_review,
                models.Review(
                    window_id=case.window.id,
                    subject_type="project",
                    subject_id=case.project.id,
                    reviewer_id=case.lead.id,
                    reviewee_id=case.member.id,
                    context="member",
                    rating=9,
                    comment="Not visible yet",
                ),
            ]
        )
        await session.flush()
    async with await harness.client(case.member) as client:
        response = await client.get("/api/v1/me/reviews/pending")
        assert response.status_code == 200, response.text
        rows = response.json()
        assert [row["id"] for row in rows] == [str(earlier.id), str(case.window.id)]
        for row in rows:
            assert row["subject_title"] == "Aurora"
            assert row["subject_type"] == "project"
            assert row["subject_id"] == str(case.project.id)
            assert row["allowed_actions"] == ["review"]
        assert rows[0]["to_review"] == [
            {"id": str(user.id), "display_name": user.display_name, "avatar_url": None}
            for user in (case.outsider, case.lead)
        ]
        assert rows[0]["submitted"] == []
        assert rows[1]["to_review"] == [
            {"id": str(case.outsider.id), "display_name": "Alex", "avatar_url": None}
        ]
        assert len(rows[1]["submitted"]) == 1
        submitted = rows[1]["submitted"][0]
        assert submitted["id"] == str(own_review.id)
        assert submitted["reviewer"] == {
            "id": str(case.member.id),
            "display_name": case.member.display_name,
            "avatar_url": None,
        }
        assert submitted["allowed_actions"] == ["edit"]
        assert submitted["revealed_at"] is None


async def test_completed_pending_window_stays_listed(
    harness: Harness, review_setup: ReviewSetup
) -> None:
    case = review_setup
    async with harness.db() as session:
        session.add(case.window)
        case.window.participants = {
            str(case.member.id): "member",
            str(case.lead.id): "lead",
            str(case.outsider.id): "member",
        }
        for peer, context, hours in ((case.lead, "lead", 1), (case.outsider, "member", 2)):
            session.add(
                models.Review(
                    window_id=case.window.id,
                    subject_type="project",
                    subject_id=case.project.id,
                    reviewer_id=case.member.id,
                    reviewee_id=peer.id,
                    context=context,
                    rating=8,
                    comment="Helpful",
                    created_at=harness.now - timedelta(hours=hours),
                )
            )
    async with await harness.client(case.member) as client:
        response = await client.get("/api/v1/me/reviews/pending")
        assert response.status_code == 200, response.text
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["id"] == str(case.window.id)
        assert rows[0]["to_review"] == []
        assert rows[0]["allowed_actions"] == ["review"]
        assert [review["reviewee_id"] for review in rows[0]["submitted"]] == [
            str(case.outsider.id),
            str(case.lead.id),
        ]
        assert all(review["allowed_actions"] == ["edit"] for review in rows[0]["submitted"])
