from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ErrorCode, Trust
from hmm_core import models
from hmm_core.feed import service as feed
from hmm_core.feed.schemas import FeedKind
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

ATTENDANCE = "attendance"


async def add_event(
    h: Harness,
    *,
    title: str = "Meetup",
    ends_at: datetime | None = None,
    status: str = "published",
) -> models.Event:
    async with h.db() as session:
        end = ends_at or h.now - timedelta(hours=1)
        row = models.Event(
            slug=str(uuid4()),
            kind="meetup",
            formation_enabled=False,
            title=title,
            starts_at=end - timedelta(hours=3),
            ends_at=end,
            timezone="UTC",
            language=[],
            themes=[],
            status=status,
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(row)
        await session.flush()
        return row


async def attend(
    h: Harness, event: models.Event, user: models.User, status: str = "attended"
) -> models.EventAttendance:
    async with h.db() as session:
        row = models.EventAttendance(
            event_id=event.id,
            user_id=user.id,
            status=status,
            role="attendee",
            proof_links=[],
            trust="self",
            status_at=h.now,
        )
        session.add(row)
        await session.flush()
        return row


async def add_project(h: Harness, name: str, **fields: Any) -> models.Project:
    async with h.db() as session:
        row = models.Project(
            name=name,
            description_md="Description",
            goal="Goal",
            links=[],
            visibility="public",
            status=fields.pop("status", "active"),
            origin="standalone",
            **fields,
        )
        session.add(row)
        await session.flush()
        return row


async def join(h: Harness, project: models.Project, user: models.User) -> models.ProjectMember:
    async with h.db() as session:
        row = models.ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role="member",
            state="confirmed",
            trust="self",
            joined_via="invite",
            is_team_row=False,
            joined_at=h.now - timedelta(days=3),
        )
        session.add(row)
        await session.flush()
        return row


async def recompute_jobs(h: Harness, user: models.User) -> list[dict[str, Any]]:
    return [
        job
        for job in await h.pending_jobs("recompute_member")
        if job["status"] == "todo" and job["args"]["user_id"] == str(user.id)
    ]


async def attendance_trust(h: Harness, fact_id: UUID) -> str:
    async with h.db() as session:
        row = await session.get(models.EventAttendance, fact_id)
        assert row is not None
        return row.trust


async def test_feed_endpoints_require_onboarded_member(harness: Harness) -> None:
    h = harness
    pending = await h.user(onboarded=False)
    target = uuid4()
    calls = [
        ("GET", "/api/v1/feed", None),
        ("GET", "/api/v1/me/confirmations/pending", None),
        ("POST", "/api/v1/confirmations", {"fact_type": ATTENDANCE, "fact_id": str(uuid4())}),
        ("DELETE", f"/api/v1/confirmations/{uuid4()}", None),
        ("GET", f"/api/v1/users/{target}/facts", None),
    ]
    async with h.anonymous() as anonymous, await h.client(pending) as client:
        for method, path, body in calls:
            response = await anonymous.request(method, path, json=body)
            assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
            response = await client.request(method, path, json=body)
            assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_create_confirmation(harness: Harness) -> None:
    h = harness
    a, b = await h.user(), await h.user()
    event = await add_event(h)
    await attend(h, event, a)
    fact = await attend(h, event, b)
    body = {"fact_type": ATTENDANCE, "fact_id": str(fact.id)}
    async with await h.client(a) as client:
        response = await client.post("/api/v1/confirmations", json=body)
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["source"] == "peer"
        assert created["ignored"] is False
        assert created["confirmer_id"] == str(a.id)
        assert created["subject_user_id"] == str(b.id)
        assert created["fact_type"] == ATTENDANCE
        assert created["fact_id"] == str(fact.id)
        assert created["withdrawn_at"] is None
        assert created["allowed_actions"] == ["withdraw"]
        repeated = await client.post("/api/v1/confirmations", json=body)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["id"] == created["id"]
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Confirmation)) == 1
    assert await attendance_trust(h, fact.id) == Trust.PEER
    assert len(await recompute_jobs(h, b)) == 1


async def test_create_confirmation_errors(harness: Harness) -> None:
    h = harness
    a, b, outsider = await h.user(), await h.user(), await h.user()
    event = await add_event(h)
    own = await attend(h, event, a)
    fact = await attend(h, event, b)
    going = await attend(h, await add_event(h), b, status="going")
    async with h.db() as session:
        request = models.HelpRequest(
            learner_id=b.id,
            area_id=(await h.seed_areas(("backend",)))["backend"].id,
            topic="Topic",
            goal="Goal",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=7),
        )
        session.add(request)
        await session.flush()
        mentorship = models.Mentorship(
            request_id=request.id,
            mentor_id=a.id,
            learner_id=b.id,
            area_id=request.area_id,
            kind="mentoring",
            mentor_score_at_start=Decimal("6"),
            learner_score_at_start=Decimal("3"),
            status="active",
            started_at=h.now,
            mentor_trust="self",
            learner_trust="self",
        )
        session.add(mentorship)
        await session.flush()

    def body(fact_type: str, fact_id: UUID) -> dict[str, str]:
        return {"fact_type": fact_type, "fact_id": str(fact_id)}

    async with await h.client(a) as client:
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, own.id))
        assert_problem(response, 403, ErrorCode.CANNOT_CONFIRM_OWN_FACT)
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, uuid4()))
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await client.post(
            "/api/v1/confirmations", json=body("mentorship_side", mentorship.id)
        )
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with await h.client(outsider) as client:
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, fact.id))
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with await h.client(a) as client:
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, going.id))
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, fact.id))
        assert response.status_code == 201, response.text
    async with h.db() as session:
        row = await session.get(models.EventAttendance, fact.id)
        assert row is not None
        row.status = "going"
    async with await h.client(a) as client:
        response = await client.post("/api/v1/confirmations", json=body(ATTENDANCE, fact.id))
        assert_problem(response, 409, ErrorCode.INVALID_STATE)


async def test_withdraw_confirmation(harness: Harness) -> None:
    h = harness
    a, b, other = await h.user(), await h.user(), await h.user()
    event = await add_event(h)
    await attend(h, event, a)
    fact = await attend(h, event, b)
    async with await h.client(a) as client:
        response = await client.post(
            "/api/v1/confirmations", json={"fact_type": ATTENDANCE, "fact_id": str(fact.id)}
        )
        assert response.status_code == 201, response.text
        confirmation_id = response.json()["id"]
    assert await attendance_trust(h, fact.id) == Trust.PEER
    async with await h.client(other) as client:
        response = await client.delete(f"/api/v1/confirmations/{confirmation_id}")
        assert_problem(response, 403, ErrorCode.FORBIDDEN)
    async with await h.client(a) as client:
        response = await client.delete(f"/api/v1/confirmations/{uuid4()}")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)
        response = await client.delete(f"/api/v1/confirmations/{confirmation_id}")
        assert response.status_code == 204, response.text
        assert response.content == b""
        async with h.db() as session:
            row = await session.get(models.Confirmation, UUID(confirmation_id))
            assert row is not None
            assert row.withdrawn_at == h.now
        assert await attendance_trust(h, fact.id) == Trust.SELF
        assert len(await recompute_jobs(h, b)) == 1
        response = await client.delete(f"/api/v1/confirmations/{confirmation_id}")
        assert response.status_code == 204, response.text

    late = await attend(h, event, other)
    async with await h.client(a) as client:
        response = await client.post(
            "/api/v1/confirmations", json={"fact_type": ATTENDANCE, "fact_id": str(late.id)}
        )
        assert response.status_code == 201, response.text
        late_id = response.json()["id"]
    h.advance(timedelta(days=8))
    async with await h.client(a) as client:
        response = await client.delete(f"/api/v1/confirmations/{late_id}")
        assert_problem(response, 409, ErrorCode.CONFIRMATION_WITHDRAW_CLOSED)


async def test_pending_confirmations(harness: Harness) -> None:
    h = harness
    viewer, first, second, teammate = [await h.user() for _ in range(4)]
    async with await h.client(viewer) as client:
        response = await client.get("/api/v1/me/confirmations/pending")
        assert response.status_code == 200, response.text
        assert response.json() == []
    event = await add_event(h, title="Ended")
    await attend(h, event, viewer)
    first_fact = await attend(h, event, first)
    second_fact = await attend(h, event, second)
    project = await add_project(h, "Team")
    await join(h, project, viewer)
    member = await join(h, project, teammate)
    async with await h.client(viewer) as client:
        response = await client.get("/api/v1/me/confirmations/pending")
        assert response.status_code == 200, response.text
        groups = response.json()
        assert [(group["subject_type"], group["subject_id"]) for group in groups] == [
            ("event", str(event.id)),
            ("project", str(project.id)),
        ]
        assert groups[0]["title"] == "Ended"
        assert {fact["id"] for fact in groups[0]["facts"]} == {
            str(first_fact.id),
            str(second_fact.id),
        }
        assert [fact["id"] for fact in groups[1]["facts"]] == [str(member.id)]
        assert groups[1]["facts"][0]["user_id"] == str(teammate.id)
        assert groups[1]["facts"][0]["subject_type"] == "project"
        assert all(
            fact["allowed_actions"] == ["confirm"] for group in groups for fact in group["facts"]
        )
        response = await client.post(
            "/api/v1/confirmations",
            json={"fact_type": ATTENDANCE, "fact_id": str(first_fact.id)},
        )
        assert response.status_code == 201, response.text
        response = await client.get("/api/v1/me/confirmations/pending")
        assert [fact["id"] for fact in response.json()[0]["facts"]] == [str(second_fact.id)]


async def test_user_facts(harness: Harness) -> None:
    h = harness
    viewer, target = await h.user(), await h.user()
    early = await add_event(h, title="Early", ends_at=h.now - timedelta(days=3))
    late = await add_event(h, title="Late")
    draft = await add_event(h, title="Draft", status="draft")
    early_fact = await attend(h, early, target)
    late_fact = await attend(h, late, target)
    await attend(h, draft, target)
    async with await h.client(viewer) as client:
        response = await client.get(
            f"/api/v1/users/{target.id}/facts", params={"type": "attendance", "limit": 1}
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["id"] for item in page["items"]] == [str(late_fact.id)]
        assert page["items"][0]["title"] == "Late"
        assert page["next_cursor"] is not None
        response = await client.get(
            f"/api/v1/users/{target.id}/facts",
            params={"type": "attendance", "limit": 1, "cursor": page["next_cursor"]},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["id"] for item in page["items"]] == [str(early_fact.id)]
        assert page["next_cursor"] is None
        response = await client.get(f"/api/v1/users/{target.id}/facts", params={"cursor": "broken"})
        assert_problem(response, 400, ErrorCode.BAD_REQUEST)
        response = await client.get(f"/api/v1/users/{uuid4()}/facts")
        assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_feed_listing(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas(("backend", "design"))
    viewer = await h.user()
    lead = await h.user()
    first = await add_event(h, title="First", ends_at=h.now + timedelta(days=2))
    second = await add_event(h, title="Second", ends_at=h.now + timedelta(days=3))
    hidden = await add_event(h, title="Hidden", ends_at=h.now + timedelta(days=4))
    project = await add_project(
        h, "Recruiting", status="idea", recruiting={"areas": ["design"], "note": "Join"}
    )
    async with h.db() as session:
        for index, (event, area) in enumerate(
            ((first, "backend"), (second, "design"), (hidden, "backend"))
        ):
            session.add(
                models.FeedItem(
                    kind=FeedKind.EVENT_NEW,
                    subject_type="event",
                    subject_id=event.id,
                    actor_id=None,
                    area_ids=[areas[area].id],
                    payload={"event_id": str(event.id), "event_title": event.title},
                    created_at=h.now - timedelta(hours=10 - index),
                )
            )
        session.add(
            models.AreaScore(
                user_id=viewer.id,
                area_id=areas["backend"].id,
                calculated=Decimal("7"),
                calc_reason="Score",
            )
        )
    h.advance(timedelta(minutes=1))
    async with h.db() as session:
        project_row = await session.get(models.Project, project.id)
        assert project_row is not None
        session.add(models.ProjectArea(project_id=project.id, area_id=areas["design"].id))
        await session.flush()
        await feed.record_project_recruiting(session, project_row, lead.id, h.now)
        assert await feed.hide_feed_subject(session, "event", hidden.id, h.now) == 1
    async with await h.client(viewer) as client:
        response = await client.get("/api/v1/feed")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["subject_id"] for item in items] == [
            str(project.id),
            str(second.id),
            str(first.id),
        ]
        response = await client.get("/api/v1/feed", params={"kind": "event_new"})
        assert [item["subject_id"] for item in response.json()["items"]] == [
            str(second.id),
            str(first.id),
        ]
        response = await client.get("/api/v1/feed", params={"area": "design"})
        assert [item["subject_id"] for item in response.json()["items"]] == [
            str(project.id),
            str(second.id),
        ]
        response = await client.get("/api/v1/feed", params={"mine_areas": "true"})
        assert [item["subject_id"] for item in response.json()["items"]] == [str(first.id)]
        response = await client.get("/api/v1/feed", params={"limit": 2})
        page = response.json()
        assert len(page["items"]) == 2
        assert page["next_cursor"] is not None
        response = await client.get(
            "/api/v1/feed", params={"limit": 2, "cursor": page["next_cursor"]}
        )
        page = response.json()
        assert [item["subject_id"] for item in page["items"]] == [str(first.id)]
        assert page["next_cursor"] is None
        response = await client.get("/api/v1/feed", params={"area": "unknown"})
        assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_meetup_mutual_confirmations_stay_peer(harness: Harness) -> None:
    h = harness
    members = [await h.user() for _ in range(3)]
    event = await add_event(h, title="Meetup")
    facts = {member.id: await attend(h, event, member) for member in members}
    for confirmer in members:
        async with await h.client(confirmer) as client:
            response = await client.post(
                "/api/v1/confirmations",
                json={"fact_type": ATTENDANCE, "fact_id": str(facts[confirmer.id].id)},
            )
            assert_problem(response, 403, ErrorCode.CANNOT_CONFIRM_OWN_FACT)
            for subject in members:
                if subject.id == confirmer.id:
                    continue
                response = await client.post(
                    "/api/v1/confirmations",
                    json={"fact_type": ATTENDANCE, "fact_id": str(facts[subject.id].id)},
                )
                assert response.status_code == 201, response.text
                assert response.json()["source"] == "peer"
    for member in members:
        assert await attendance_trust(h, facts[member.id].id) == Trust.PEER
        assert len(await recompute_jobs(h, member)) <= 1
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Confirmation)) == 6
