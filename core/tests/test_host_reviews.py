from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import ReviewIn
from hmm_contracts.common import ErrorCode, FlagStatus, ReviewContext, ReviewSubjectType
from hmm_core import models
from hmm_core.results.reviews import load_reviews_received
from hmm_core.results.schemas import Review
from hmm_core.results.windows import reveal_due_windows
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


@dataclass(frozen=True)
class HostReviewSetup:
    event: models.Event
    attendee: models.User
    host: models.User
    speaker: models.User
    no_show: models.User


async def create_event(harness: Harness, *, slug: str, starts_at: datetime) -> models.Event:
    async with harness.db() as session:
        event = models.Event(
            slug=slug,
            kind="meetup",
            formation_enabled=False,
            title="NL 2026",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
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
    return event


@pytest.fixture
async def host_review_setup(harness: Harness) -> HostReviewSetup:
    event = await create_event(harness, slug="nl-2026", starts_at=harness.now - timedelta(hours=1))
    attendee = await harness.user(login="attendee")
    host = await harness.user(login="host")
    speaker = await harness.user(login="speaker")
    no_show = await harness.user(login="no-show")
    async with harness.db() as session:
        for user, status, role in (
            (attendee, "attended", "attendee"),
            (host, "attended", "host"),
            (speaker, "attended", "speaker"),
            (no_show, "going", "attendee"),
        ):
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=user.id,
                    status=status,
                    role=role,
                    proof_links=[],
                    trust="self",
                    status_at=harness.now,
                )
            )
    return HostReviewSetup(event, attendee, host, speaker, no_show)


async def test_host_and_speaker_ratings_are_instant_without_side_effects(
    harness: Harness, host_review_setup: HostReviewSetup
) -> None:
    case = host_review_setup
    now = harness.now
    path = f"/api/v1/events/{case.event.id}/host-reviews"
    async with await harness.client(case.attendee) as client:
        response = await client.put(
            f"{path}/{case.host.id}", json={"rating": 9, "comment": " Great session "}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        initial = Review.model_validate(body)
        assert initial.window_id is None
        assert initial.subject_type == ReviewSubjectType.EVENT
        assert initial.subject_id == case.event.id
        assert initial.context == ReviewContext.HOST
        assert initial.rating == 9
        assert initial.comment == "Great session"
        assert initial.revealed_at == now
        assert body["reviewer"]["id"] == str(case.attendee.id)
        assert body["allowed_actions"] == []
        async with harness.db() as session:
            row = (await session.scalars(select(models.Review))).one()
            assert row.id == initial.id
            assert row.context == "host"
            assert row.subject_type == "event"
            assert row.subject_id == case.event.id
            assert row.window_id is None
            assert row.revealed_at == now
        harness.advance(timedelta(minutes=1))
        response = await client.put(
            f"{path}/{case.host.id}", json={"rating": 7, "comment": "Still good"}
        )
        assert response.status_code == 200, response.text
        edited = Review.model_validate(response.json())
        assert edited.id == initial.id
        assert edited.rating == 7
        assert edited.comment == "Still good"
        assert edited.revealed_at == now
        assert edited.created_at == initial.created_at
        assert response.json()["allowed_actions"] == []
        async with harness.db() as session:
            row = (await session.scalars(select(models.Review))).one()
            assert row.rating == 7
            assert row.updated_at == edited.updated_at
        response = await client.put(
            f"{path}/{case.speaker.id}", json={"rating": 8, "comment": "Useful talk"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["context"] == "speaker"
        assert Review.model_validate(response.json()).revealed_at == harness.now
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Review)) == 2
        for model in (
            models.MemberMetric,
            models.FactAward,
            models.XpAdjustment,
            models.ActivityEntry,
            models.Notification,
            models.AuditLog,
            models.ReviewWindow,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert await harness.pending_jobs() == []


@pytest.mark.parametrize(
    ("scenario", "status", "code"),
    (
        ("no_show", 403, ErrorCode.FORBIDDEN),
        ("missing_reviewer", 403, ErrorCode.FORBIDDEN),
        ("missing_event", 404, ErrorCode.NOT_FOUND),
        ("attendee_target", 404, ErrorCode.NOT_FOUND),
        ("missing_target", 404, ErrorCode.NOT_FOUND),
        ("cancelled_target", 404, ErrorCode.NOT_FOUND),
        ("self", 403, ErrorCode.FORBIDDEN),
    ),
)
async def test_host_rating_eligibility(
    harness: Harness,
    host_review_setup: HostReviewSetup,
    scenario: str,
    status: int,
    code: ErrorCode,
) -> None:
    case = host_review_setup
    actor = case.attendee
    if scenario == "no_show":
        actor = case.no_show
    elif scenario == "missing_reviewer":
        actor = await harness.user(login="absent")
    elif scenario == "self":
        actor = case.host
    target_id = case.attendee.id if scenario == "attendee_target" else case.host.id
    if scenario == "missing_target":
        target_id = (await harness.user(login="absent")).id
    if scenario == "cancelled_target":
        async with harness.db() as session:
            target = await session.scalar(
                select(models.EventAttendance).where(
                    models.EventAttendance.event_id == case.event.id,
                    models.EventAttendance.user_id == case.host.id,
                )
            )
            assert target is not None
            target.status = "cancelled"
    event_id = uuid4() if scenario == "missing_event" else case.event.id
    async with await harness.client(actor) as client:
        problem = assert_problem(
            await client.put(
                f"/api/v1/events/{event_id}/host-reviews/{target_id}",
                json={"rating": 9, "comment": "Great session"},
            ),
            status,
            code,
        )
        if scenario in {"no_show", "missing_reviewer"}:
            assert problem.detail == "Only attendees who attended can rate"
        elif scenario == "self":
            assert problem.detail == "You cannot review yourself"
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Review)) == 0


async def test_host_reviews_open_exactly_when_event_starts(
    harness: Harness, host_review_setup: HostReviewSetup
) -> None:
    case = host_review_setup
    event = await create_event(harness, slug="future", starts_at=harness.now + timedelta(hours=1))
    async with harness.db() as session:
        for user, role in ((case.host, "host"), (case.attendee, "attendee")):
            session.add(
                models.EventAttendance(
                    event_id=event.id,
                    user_id=user.id,
                    status="attended",
                    role=role,
                    proof_links=[],
                    trust="self",
                    status_at=harness.now,
                )
            )
    async with await harness.client(case.attendee) as client:
        path = f"/api/v1/events/{event.id}/host-reviews/{case.host.id}"
        problem = assert_problem(
            await client.put(path, json={"rating": 9, "comment": "Great session"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        assert problem.detail == "Host reviews open when the event starts"
        harness.advance(timedelta(hours=1))
        response = await client.put(path, json={"rating": 9, "comment": "Great session"})
        assert response.status_code == 200, response.text
        assert Review.model_validate(response.json()).revealed_at == event.starts_at


@pytest.mark.parametrize(
    "body",
    (
        {"rating": 9, "comment": "   "},
        {"rating": 0, "comment": "Helpful"},
        {"rating": 11, "comment": "Helpful"},
        {"rating": None, "comment": "Helpful"},
        {"comment": "Helpful"},
    ),
)
async def test_host_reviews_require_valid_rating_and_comment(
    harness: Harness,
    host_review_setup: HostReviewSetup,
    body: dict[str, int | str | None],
) -> None:
    case = host_review_setup
    async with await harness.client(case.attendee) as client:
        problem = assert_problem(
            await client.put(
                f"/api/v1/events/{case.event.id}/host-reviews/{case.host.id}", json=body
            ),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        if body.get("comment") == "   ":
            assert problem.detail == "Comment must not be empty"
    async with harness.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Review)) == 0


async def test_public_reviews_anonymity_reveal_filters_and_pagination(
    harness: Harness, host_review_setup: HostReviewSetup
) -> None:
    case = host_review_setup
    async with await harness.client(case.attendee) as client:
        response = await client.put(
            f"/api/v1/events/{case.event.id}/host-reviews/{case.host.id}",
            json={"rating": 9, "comment": "Great session"},
        )
        assert response.status_code == 200, response.text
        host_review_id = response.json()["id"]
    async with harness.db() as session:
        project = models.Project(
            name="Aurora",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="active",
            origin="standalone",
            lead_user_id=case.host.id,
        )
        session.add(project)
        await session.flush()
        window = models.ReviewWindow(
            subject_type="project",
            subject_id=project.id,
            opens_at=harness.now,
            closes_at=harness.now + timedelta(days=7),
            participants={
                str(case.host.id): "member",
                str(case.attendee.id): "member",
                str(case.speaker.id): "member",
            },
        )
        session.add(window)
        await session.flush()
        member_review = models.Review(
            window_id=window.id,
            subject_type="project",
            subject_id=project.id,
            reviewer_id=case.attendee.id,
            reviewee_id=case.host.id,
            context="member",
            rating=8,
            comment="Helpful teammate",
            created_at=harness.now,
        )
        removed_review = models.Review(
            window_id=window.id,
            subject_type="project",
            subject_id=project.id,
            reviewer_id=case.speaker.id,
            reviewee_id=case.host.id,
            context="member",
            rating=2,
            comment="Removed",
            revealed_at=harness.now,
            removed_at=harness.now,
            created_at=harness.now,
        )
        speaker_review = models.Review(
            subject_type="event",
            subject_id=uuid4(),
            reviewer_id=case.speaker.id,
            reviewee_id=case.host.id,
            context="speaker",
            rating=5,
            comment="Useful talk",
            revealed_at=harness.now,
            created_at=harness.now,
        )
        session.add_all([member_review, removed_review, speaker_review])
        await session.flush()
    path = f"/api/v1/users/{case.host.id}/reviews"
    async with await harness.client(case.no_show) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert {item["id"] for item in items} == {host_review_id, str(speaker_review.id)}
        assert all(item["reviewer"] is None for item in items)
        assert all(item["allowed_actions"] == ["report"] for item in items)
        assert {item["comment"] for item in items} == {"Great session", "Useful talk"}
        assert {item["rating"] for item in items} == {9, 5}
        response = await client.get(path, params={"context": "host"})
        assert response.status_code == 200, response.text
        assert {item["context"] for item in response.json()["items"]} == {"host", "speaker"}
        response = await client.get(path, params={"context": "speaker"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(speaker_review.id)]
        response = await client.get(path, params={"context": "member"})
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
    async with await harness.client(case.attendee) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        own = next(item for item in response.json()["items"] if item["id"] == host_review_id)
        assert own["reviewer"] is None
        assert own["allowed_actions"] == []
    harness.advance(timedelta(days=8))
    async with harness.db() as session:
        assert await reveal_due_windows(session, harness.now) == 1
    async with await harness.client(case.no_show) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert {item["id"] for item in items} == {
            host_review_id,
            str(speaker_review.id),
            str(member_review.id),
        }
        member = next(item for item in items if item["id"] == str(member_review.id))
        assert member["reviewer"] == {
            "id": str(case.attendee.id),
            "display_name": case.attendee.display_name,
            "avatar_url": None,
        }
        assert member["allowed_actions"] == ["report"]
        response = await client.get(path, params={"context": "member"})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(member_review.id)]
        page_ids: list[str] = []
        cursor: str | None = None
        for index in range(3):
            params = {"limit": "1"}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get(path, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["items"]) == 1
            page_ids.append(page["items"][0]["id"])
            cursor = page["next_cursor"]
            assert (cursor is not None) == (index < 2)
        assert page_ids == [item["id"] for item in items]
        assert len(set(page_ids)) == 3


@pytest.mark.parametrize("missing", (True, False))
async def test_review_list_hides_nonmember_targets(
    harness: Harness, host_review_setup: HostReviewSetup, missing: bool
) -> None:
    case = host_review_setup
    target_id = uuid4() if missing else (await harness.user(onboarded=False)).id
    async with await harness.client(case.attendee) as client:
        assert_problem(
            await client.get(f"/api/v1/users/{target_id}/reviews"), 404, ErrorCode.NOT_FOUND
        )


async def test_review_list_uses_reputation_threshold_and_context_order(
    harness: Harness, host_review_setup: HostReviewSetup
) -> None:
    case = host_review_setup
    path = f"/api/v1/users/{case.host.id}/reviews"
    async with await harness.client(case.no_show) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "next_cursor": None, "reputation": []}
    for actor, rating in ((case.attendee, 9), (case.speaker, 7)):
        async with await harness.client(actor) as client:
            response = await client.put(
                f"/api/v1/events/{case.event.id}/host-reviews/{case.host.id}",
                json={"rating": rating, "comment": "Great session"},
            )
            assert response.status_code == 200, response.text
    async with await harness.client(case.no_show) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["reputation"] == [{"context": "host", "average": None, "count": 2}]
    async with harness.db() as session:
        session.add_all(
            [
                models.Review(
                    subject_type="event",
                    subject_id=uuid4(),
                    reviewer_id=case.no_show.id,
                    reviewee_id=case.host.id,
                    context="speaker",
                    rating=5,
                    comment="Useful talk",
                    revealed_at=harness.now,
                ),
                models.Review(
                    subject_type="project",
                    subject_id=uuid4(),
                    reviewer_id=case.attendee.id,
                    reviewee_id=case.host.id,
                    context="member",
                    rating=8,
                    comment="Helpful teammate",
                    revealed_at=harness.now,
                ),
            ]
        )
    async with await harness.client(case.no_show) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["reputation"] == [
            {"context": "member", "average": 8.0, "count": 1},
            {"context": "host", "average": 7.0, "count": 3},
        ]
        response = await client.get(path, params={"context": "member", "limit": 1})
        assert response.status_code == 200, response.text
        assert response.json()["reputation"] == [
            {"context": "member", "average": 8.0, "count": 1},
            {"context": "host", "average": 7.0, "count": 3},
        ]


async def test_raw_reviews_preserve_endorsements_and_live_flag_state(
    harness: Harness, host_review_setup: HostReviewSetup
) -> None:
    case = host_review_setup
    async with harness.db() as session:
        host_review = models.Review(
            subject_type="event",
            subject_id=case.event.id,
            reviewer_id=case.attendee.id,
            reviewee_id=case.host.id,
            context="host",
            rating=9,
            comment="Great session",
            created_at=harness.now - timedelta(hours=2),
            revealed_at=harness.now,
        )
        endorsement = models.Review(
            subject_type="mentorship",
            subject_id=uuid4(),
            reviewer_id=case.speaker.id,
            reviewee_id=case.host.id,
            context="mentor",
            rating=None,
            comment="Steady progress",
            created_at=harness.now - timedelta(hours=1),
            revealed_at=harness.now,
        )
        attendee_review = models.Review(
            subject_type="project",
            subject_id=uuid4(),
            reviewer_id=case.host.id,
            reviewee_id=case.attendee.id,
            context="member",
            rating=8,
            comment="Helpful teammate",
            created_at=harness.now,
            revealed_at=harness.now,
        )
        session.add_all(
            [
                endorsement,
                host_review,
                attendee_review,
                models.Review(
                    subject_type="project",
                    subject_id=uuid4(),
                    reviewer_id=case.attendee.id,
                    reviewee_id=case.host.id,
                    context="member",
                    rating=1,
                    comment="Still blind",
                ),
                models.Review(
                    subject_type="project",
                    subject_id=uuid4(),
                    reviewer_id=case.speaker.id,
                    reviewee_id=case.host.id,
                    context="member",
                    rating=2,
                    comment="Removed",
                    revealed_at=harness.now,
                    removed_at=harness.now,
                ),
            ]
        )
        await session.flush()
        assert await load_reviews_received(session, []) == {}
        received = await load_reviews_received(
            session, [case.host.id, case.attendee.id, case.no_show.id]
        )
        assert set(received) == {case.host.id, case.attendee.id, case.no_show.id}
        assert received[case.no_show.id] == []
        assert [review.id for review in received[case.host.id]] == [host_review.id, endorsement.id]
        assert [review.id for review in received[case.attendee.id]] == [attendee_review.id]
        assert received[case.host.id][1].rating is None
        assert received[case.host.id][0] == ReviewIn(
            id=host_review.id,
            reviewer_id=case.attendee.id,
            reviewee_id=case.host.id,
            context=ReviewContext.HOST,
            rating=9,
            subject_type=ReviewSubjectType.EVENT,
            subject_id=case.event.id,
            comment="Great session",
            created_at=host_review.created_at,
            revealed_at=harness.now,
            removed_at=None,
            flag_status=None,
        )
        assert all(
            review.flag_status is None for reviews in received.values() for review in reviews
        )
        flag = models.Flag(
            user_id=case.host.id,
            subject_type="review",
            subject_id=host_review.id,
            related_user_ids=[],
            reason="ring",
            severity="medium",
            status="to_check",
            algo_version="test",
        )
        session.add_all(
            [
                flag,
                models.Flag(
                    user_id=case.host.id,
                    subject_type="attendance",
                    subject_id=endorsement.id,
                    related_user_ids=[],
                    reason="Other subject type",
                    severity="medium",
                    status="to_check",
                    algo_version="test",
                ),
            ]
        )
        await session.flush()
        received = await load_reviews_received(session, [case.host.id, case.attendee.id])
        assert received[case.host.id][0].flag_status == FlagStatus.TO_CHECK
        assert received[case.host.id][1].flag_status is None
        flag.status = "pardoned"
        await session.flush()
        received = await load_reviews_received(session, [case.host.id, case.attendee.id])
        assert all(
            review.flag_status is None for reviews in received.values() for review in reviews
        )
