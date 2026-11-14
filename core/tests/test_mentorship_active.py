from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import select
from test_api_skeleton import assert_problem

MINE = "/api/v1/me/mentorships"


def url(mentorship_id: UUID) -> str:
    return f"/api/v1/mentorships/{mentorship_id}"


def checkins_url(mentorship_id: UUID) -> str:
    return f"/api/v1/mentorships/{mentorship_id}/checkins"


async def add_mentorship(
    h: Harness,
    mentor: models.User,
    learner: models.User,
    *,
    status: str = "active",
    started_at: datetime | None = None,
    **fields: Any,
) -> UUID:
    async with h.db() as session:
        area_id = await session.scalar(select(models.Area.id).order_by(models.Area.sort).limit(1))
        assert area_id is not None
        request = models.HelpRequest(
            learner_id=learner.id,
            area_id=area_id,
            topic="FastAPI auth",
            goal="ship a login flow",
            format="online",
            status="matched",
            expires_at=h.now + timedelta(days=14),
        )
        session.add(request)
        await session.flush()
        row = models.Mentorship(
            request_id=request.id,
            mentor_id=mentor.id,
            learner_id=learner.id,
            area_id=area_id,
            kind="mentoring",
            mentor_score_at_start=Decimal("5.0"),
            learner_score_at_start=Decimal("5.0"),
            status=status,
            started_at=started_at or h.now - timedelta(days=30),
            mentor_trust="self",
            learner_trust="self",
            **fields,
        )
        session.add(row)
        await session.flush()
        return row.id


async def mentorship_row(h: Harness, mentorship_id: UUID) -> models.Mentorship:
    async with h.db() as session:
        row = await session.get(models.Mentorship, mentorship_id)
        assert row is not None
        return row


def checkin(occurred_on: str, **fields: Any) -> dict[str, Any]:
    return {"note": "fixed auth", "occurred_on": occurred_on, "minutes": 45, **fields}


async def setup(h: Harness) -> tuple[models.User, models.User, models.User, models.User]:
    await h.seed_areas()
    learner = await h.user(login="learner")
    mentor = await h.user(login="mentor")
    other = await h.user(login="other")
    admin = await h.admin()
    return learner, mentor, other, admin


async def test_list_my_mentorships(harness: Harness) -> None:
    h = harness
    learner, mentor, other, admin = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner)
    async with await h.client(learner) as c:
        response = await c.get(MINE)
        assert response.status_code == 200, response.text
        items = response.json()
        assert len(items) == 1
        assert items[0]["id"] == str(mentorship_id)
        assert items[0]["allowed_actions"] == ["check_in", "close", "cancel"]
        assert items[0]["checkins"] == []
        assert (await c.get(MINE, params={"role": "mentor"})).json() == []
        assert len((await c.get(MINE, params={"role": "learner"})).json()) == 1
        assert (await c.get(MINE, params={"status": "closed"})).json() == []
        assert len((await c.get(MINE, params={"status": "active"})).json()) == 1
        assert_problem(
            await c.get(MINE, params={"status": "bogus"}), 422, ErrorCode.VALIDATION_ERROR
        )
    async with await h.client(mentor) as c:
        items = (await c.get(MINE)).json()
        assert [item["id"] for item in items] == [str(mentorship_id)]
        assert items[0]["allowed_actions"] == ["check_in", "propose_close", "cancel"]
    for user in (other, admin):
        async with await h.client(user) as c:
            response = await c.get(MINE)
            assert response.status_code == 200, response.text
            assert response.json() == []
    async with h.anonymous() as c:
        assert_problem(await c.get(MINE), 401, ErrorCode.UNAUTHENTICATED)
    newcomer = await h.user(onboarded=False)
    async with await h.client(newcomer) as c:
        assert_problem(await c.get(MINE), 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_get_mentorship(harness: Harness) -> None:
    h = harness
    learner, mentor, other, admin = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner)
    async with await h.client(learner) as c:
        response = await c.get(url(mentorship_id))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(mentorship_id)
        assert body["mentor_id"] == str(mentor.id)
        assert body["learner_id"] == str(learner.id)
        assert body["kind"] == "mentoring"
        assert body["status"] == "active"
        assert body["outcome"] is None
    async with await h.client(mentor) as c:
        assert (await c.get(url(mentorship_id))).status_code == 200
    async with await h.client(admin) as c:
        response = await c.get(url(mentorship_id))
        assert response.status_code == 200, response.text
        assert response.json()["allowed_actions"] == []
    async with await h.client(other) as c:
        assert_problem(await c.get(url(mentorship_id)), 404, ErrorCode.NOT_FOUND)
        assert_problem(await c.get(url(uuid4())), 404, ErrorCode.NOT_FOUND)


async def test_checkin_happy_path(harness: Harness) -> None:
    h = harness
    learner, mentor, _, _ = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner)
    async with await h.client(learner) as c:
        response = await c.post(checkins_url(mentorship_id), json=checkin(h.now.date().isoformat()))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["author_id"] == str(learner.id)
        assert body["mentorship_id"] == str(mentorship_id)
        assert body["minutes"] == 45
    async with h.db() as session:
        row = await session.get(models.MentorshipCheckin, UUID(body["id"]))
        assert row is not None
        assert row.note == "fixed auth"
    assert (await mentorship_row(h, mentorship_id)).last_checkin_at == h.now
    jobs = await h.pending_jobs("recompute_member")
    assert len(jobs) == 2
    assert {job["args"]["user_id"] for job in jobs} == {str(mentor.id), str(learner.id)}


async def test_checkin_conflicts(harness: Harness) -> None:
    h = harness
    learner, mentor, _, _ = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner)
    today = h.now.date().isoformat()
    async with await h.client(learner) as c:
        assert (await c.post(checkins_url(mentorship_id), json=checkin(today))).status_code == 201
        assert_problem(
            await c.post(checkins_url(mentorship_id), json=checkin(today)),
            409,
            ErrorCode.CHECKIN_LIMIT,
        )
    async with await h.client(mentor) as c:
        assert (await c.post(checkins_url(mentorship_id), json=checkin(today))).status_code == 201
    h.advance(timedelta(days=1))
    async with await h.client(learner) as c:
        response = await c.post(checkins_url(mentorship_id), json=checkin(h.now.date().isoformat()))
        assert response.status_code == 201, response.text


async def test_checkin_validation(harness: Harness) -> None:
    h = harness
    learner, mentor, _, _ = await setup(h)
    started_at = h.now - timedelta(days=30)
    mentorship_id = await add_mentorship(h, mentor, learner, started_at=started_at)
    today = h.now.date()
    async with await h.client(learner) as c:
        for payload in (
            checkin((today + timedelta(days=1)).isoformat()),
            checkin((started_at.date() - timedelta(days=1)).isoformat()),
            checkin(today.isoformat(), minutes=-1),
            checkin(today.isoformat(), note=""),
        ):
            assert_problem(
                await c.post(checkins_url(mentorship_id), json=payload),
                422,
                ErrorCode.VALIDATION_ERROR,
            )


async def test_checkin_roles_and_state(harness: Harness) -> None:
    h = harness
    learner, mentor, other, admin = await setup(h)
    mentorship_id = await add_mentorship(h, mentor, learner)
    today = h.now.date().isoformat()
    for user in (other, admin):
        async with await h.client(user) as c:
            assert_problem(
                await c.post(checkins_url(mentorship_id), json=checkin(today)),
                404,
                ErrorCode.NOT_FOUND,
            )
    second = await h.user(login="second")
    closed_id = await add_mentorship(h, mentor, second, status="closed", closed_at=h.now)
    third = await h.user(login="third")
    cancelled_id = await add_mentorship(h, mentor, third, status="cancelled", closed_at=h.now)
    for target, learner_user in ((closed_id, second), (cancelled_id, third)):
        for user in (mentor, learner_user):
            async with await h.client(user) as c:
                assert_problem(
                    await c.post(checkins_url(target), json=checkin(today)),
                    409,
                    ErrorCode.INVALID_STATE,
                )


async def test_checkin_reactivates_stalled(harness: Harness) -> None:
    h = harness
    learner, mentor, _, _ = await setup(h)
    mentorship_id = await add_mentorship(
        h,
        mentor,
        learner,
        status="stalled",
        stalled_at=h.now - timedelta(days=3),
        last_checkin_at=None,
    )
    async with await h.client(mentor) as c:
        response = await c.get(url(mentorship_id))
        assert response.status_code == 200, response.text
        assert "check_in" in response.json()["allowed_actions"]
        response = await c.post(checkins_url(mentorship_id), json=checkin(h.now.date().isoformat()))
        assert response.status_code == 201, response.text
    row = await mentorship_row(h, mentorship_id)
    assert row.status == "active"
    assert row.stalled_at is None
    assert row.last_checkin_at == h.now
