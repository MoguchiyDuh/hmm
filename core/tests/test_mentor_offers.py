from uuid import UUID

from harness import Harness
from hmm_contracts.common import ErrorCode
from hmm_core import models
from sqlalchemy import select, update
from test_api_skeleton import assert_problem

URL = "/api/v1/me/mentor-offers"

INITIAL = {
    "capacity": 0,
    "offers": [
        {"area_key": "ml_ai", "topics": " Kaggle ", "active": True},
        {"area_key": "backend", "topics": "auth", "active": False},
    ],
}

STORED = [
    {"area_key": "backend", "topics": "auth", "active": False},
    {"area_key": "ml_ai", "topics": "Kaggle", "active": True},
]


async def stored_offers(h: Harness, user_id: UUID) -> dict[UUID, tuple[str, bool]]:
    async with h.db() as session:
        rows = await session.scalars(
            select(models.MentorOffer).where(models.MentorOffer.user_id == user_id)
        )
        return {row.area_id: (row.topics, row.active) for row in rows}


async def stored_capacity(h: Harness, user_id: UUID) -> int | None:
    async with h.db() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        return user.mentor_capacity


async def test_fresh_member_has_no_offers(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    member = await h.user(login="fresh")
    async with await h.client(member) as c:
        response = await c.get(URL)
    assert response.status_code == 200, response.text
    assert response.json() == {"capacity": None, "offers": [], "allowed_actions": ["edit"]}


async def test_replace_and_shrink_offers(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user(login="mentor")
    async with await h.client(member) as c:
        response = await c.put(URL, json=INITIAL)
        assert response.status_code == 200, response.text
        assert response.json() == {"capacity": 0, "offers": STORED, "allowed_actions": ["edit"]}
        assert await stored_offers(h, member.id) == {
            areas["backend"].id: ("auth", False),
            areas["ml_ai"].id: ("Kaggle", True),
        }
        assert await stored_capacity(h, member.id) == 0
        listed = await c.get(URL)
        assert listed.status_code == 200, listed.text
        assert listed.json()["offers"] == STORED

        response = await c.put(
            URL,
            json={
                "capacity": None,
                "offers": [{"area_key": "backend", "topics": "auth, sql", "active": False}],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "capacity": None,
            "offers": [{"area_key": "backend", "topics": "auth, sql", "active": False}],
            "allowed_actions": ["edit"],
        }
    assert await stored_offers(h, member.id) == {areas["backend"].id: ("auth, sql", False)}
    assert await stored_capacity(h, member.id) is None


async def test_invalid_replacements_leave_offers_unchanged(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user(login="careful")
    async with await h.client(member) as c:
        assert (await c.put(URL, json=INITIAL)).status_code == 200
        before = await stored_offers(h, member.id)

        duplicate = {
            "capacity": 5,
            "offers": [
                {"area_key": "data", "topics": "sql", "active": True},
                {"area_key": "data", "topics": "bi", "active": True},
            ],
        }
        assert_problem(await c.put(URL, json=duplicate), 422, ErrorCode.VALIDATION_ERROR)
        assert await stored_offers(h, member.id) == before
        assert await stored_capacity(h, member.id) == 0

        unknown = {
            "capacity": 5,
            "offers": [
                {"area_key": "data", "topics": "sql", "active": True},
                {"area_key": "nope", "topics": "x", "active": True},
            ],
        }
        assert_problem(await c.put(URL, json=unknown), 404, ErrorCode.NOT_FOUND)
        assert await stored_offers(h, member.id) == before
        assert await stored_capacity(h, member.id) == 0

        async with h.db() as session:
            await session.execute(
                update(models.Area)
                .where(models.Area.id == areas["qa"].id)
                .values(archived_at=h.now)
            )
        archived = {
            "capacity": 5,
            "offers": [{"area_key": "qa", "topics": "pytest", "active": True}],
        }
        assert_problem(await c.put(URL, json=archived), 404, ErrorCode.NOT_FOUND)
        assert await stored_offers(h, member.id) == before
        assert await stored_capacity(h, member.id) == 0


async def test_archived_area_offer_hidden_and_deleted(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user(login="archivist")
    async with h.db() as session:
        session.add(
            models.MentorOffer(
                user_id=member.id, area_id=areas["gamedev"].id, topics="unity", active=True
            )
        )
        session.add(
            models.MentorOffer(
                user_id=member.id, area_id=areas["design"].id, topics="figma", active=True
            )
        )
        await session.execute(
            update(models.Area)
            .where(models.Area.id == areas["gamedev"].id)
            .values(archived_at=h.now)
        )
    async with await h.client(member) as c:
        listed = await c.get(URL)
        assert listed.status_code == 200, listed.text
        assert listed.json()["offers"] == [
            {"area_key": "design", "topics": "figma", "active": True}
        ]
        response = await c.put(
            URL,
            json={
                "capacity": 2,
                "offers": [{"area_key": "design", "topics": "figma", "active": True}],
            },
        )
        assert response.status_code == 200, response.text
    assert await stored_offers(h, member.id) == {areas["design"].id: ("figma", True)}
    assert await stored_capacity(h, member.id) == 2


async def test_mentor_offers_auth(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    async with h.anonymous() as c:
        assert_problem(await c.get(URL), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(await c.put(URL, json=INITIAL), 401, ErrorCode.UNAUTHENTICATED)
    newcomer = await h.user(login="newcomer", onboarded=False)
    async with await h.client(newcomer) as c:
        assert_problem(await c.get(URL), 403, ErrorCode.ONBOARDING_REQUIRED)
        assert_problem(await c.put(URL, json=INITIAL), 403, ErrorCode.ONBOARDING_REQUIRED)
