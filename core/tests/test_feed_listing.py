from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.common import AreaKey, ErrorCode
from hmm_core import models
from hmm_core.feed.listing import list_feed
from hmm_core.feed.schemas import FeedItemPage, FeedKind
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def page(
    h: Harness,
    viewer: models.User,
    *,
    kind: FeedKind | None = None,
    area: AreaKey | None = None,
    mine_areas: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> FeedItemPage:
    async with h.db() as session:
        return await list_feed(
            session,
            viewer,
            CursorParams(cursor=cursor, limit=limit),
            kind=kind,
            area=area,
            mine_areas=mine_areas,
            now=h.now,
        )


async def project(
    session: AsyncSession, *, visibility: str = "public", lead_id: UUID | None = None
) -> models.Project:
    row = models.Project(
        name="Project",
        description_md="Description",
        goal="Ship",
        links=[],
        visibility=visibility,
        status="idea",
        origin="standalone",
        recruiting={"areas": ["backend"], "note": "Join"},
        lead_user_id=lead_id,
    )
    session.add(row)
    await session.flush()
    return row


async def event(
    session: AsyncSession, now: datetime, *, status: str = "published", days: int = 1
) -> models.Event:
    row = models.Event(
        slug=f"event-{uuid4()}",
        kind="meetup",
        formation_enabled=False,
        title="Event",
        starts_at=now + timedelta(days=days - 1),
        ends_at=now + timedelta(days=days),
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


async def help_request(
    session: AsyncSession,
    learner: models.User,
    area: models.Area,
    now: datetime,
    *,
    status: str = "open",
) -> models.HelpRequest:
    row = models.HelpRequest(
        learner_id=learner.id,
        area_id=area.id,
        topic="Topic",
        goal="Learn",
        format="online",
        status=status,
        expires_at=now + timedelta(days=14),
    )
    session.add(row)
    await session.flush()
    return row


async def mentorship(
    session: AsyncSession,
    mentor: models.User,
    learner: models.User,
    area: models.Area,
    now: datetime,
    *,
    status: str = "closed",
    outcome: str | None = "reached",
) -> models.Mentorship:
    request = await help_request(session, learner, area, now, status="matched")
    row = models.Mentorship(
        request_id=request.id,
        mentor_id=mentor.id,
        learner_id=learner.id,
        area_id=area.id,
        kind="mentoring",
        mentor_score_at_start=Decimal("8"),
        learner_score_at_start=Decimal("3"),
        status=status,
        started_at=now,
        mentor_trust="self",
        learner_trust="self",
        outcome=outcome,
    )
    session.add(row)
    await session.flush()
    return row


def feed_item(
    session: AsyncSession,
    kind: FeedKind,
    subject_id: UUID,
    areas: list[models.Area],
    now: datetime,
    *,
    actor: models.User | None = None,
    hidden: bool = False,
) -> models.FeedItem:
    payload: dict[str, JsonValue]
    if kind == FeedKind.PROJECT_RECRUITING:
        subject_type = "project"
        payload = {"project_id": str(subject_id), "name": "Project", "goal": "Ship"}
    elif kind == FeedKind.PROJECT_SHIPPED:
        subject_type = "project"
        payload = {
            "project_id": str(subject_id),
            "name": "Project",
            "release_id": None,
            "release_title": None,
        }
    elif kind in (FeedKind.EVENT_GOING, FeedKind.EVENT_NEW):
        subject_type = "event"
        payload = {"event_id": str(subject_id), "event_title": "Stored event title"}
        if kind == FeedKind.EVENT_GOING:
            payload["count"] = 99
    elif kind == FeedKind.HELP_REQUEST:
        assert actor is not None
        subject_type = "help_request"
        payload = {
            "request_id": str(subject_id),
            "area_key": areas[0].key,
            "topic": "Topic",
            "goal": "Learn",
            "learner_id": str(actor.id),
        }
    else:
        subject_type = "mentorship"
        payload = {
            "mentorship_id": str(subject_id),
            "area_key": areas[0].key,
            "mentor": {"user_id": str(uuid4()), "display_name": "Mentor"},
            "learner": {"user_id": str(uuid4()), "display_name": "Learner"},
        }
    row = models.FeedItem(
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_id=actor.id if actor is not None else None,
        area_ids=[area.id for area in areas],
        payload=payload,
        created_at=now,
        hidden_at=now if hidden else None,
    )
    session.add(row)
    return row


def attendance(
    session: AsyncSession, event: models.Event, user: models.User, now: datetime
) -> models.EventAttendance:
    row = models.EventAttendance(
        event_id=event.id,
        user_id=user.id,
        status="going",
        role="attendee",
        proof_links=[],
        trust="self",
        status_at=now,
    )
    session.add(row)
    return row


def member(
    session: AsyncSession, project: models.Project, user: models.User, now: datetime
) -> models.ProjectMember:
    row = models.ProjectMember(
        project_id=project.id,
        user_id=user.id,
        role="member",
        state="confirmed",
        trust="self",
        joined_via="join_request",
        is_team_row=False,
        joined_at=now,
    )
    session.add(row)
    return row


async def test_feed_kind_area_hidden_and_actor(harness: Harness) -> None:
    h = harness
    viewer = await h.user(avatar_url="https://hmm.test/avatar.png")
    areas = await h.seed_areas(("backend", "design"))
    async with h.db() as session:
        p = await project(session)
        e = await event(session, h.now)
        recruiting = feed_item(
            session, FeedKind.PROJECT_RECRUITING, p.id, [areas["backend"]], h.now, actor=viewer
        )
        shipped = feed_item(session, FeedKind.PROJECT_SHIPPED, p.id, [areas["design"]], h.now)
        new = feed_item(session, FeedKind.EVENT_NEW, e.id, [areas["backend"]], h.now)
        feed_item(session, FeedKind.EVENT_NEW, e.id, [areas["design"]], h.now, hidden=True)
        feed_item(session, FeedKind.PROJECT_SHIPPED, uuid4(), [], h.now)
    all_items = (await page(h, viewer)).items
    assert {item.id for item in all_items} == {recruiting.id, shipped.id, new.id}
    assert all(item.hidden_at is None for item in all_items)
    actor_item = next(item for item in all_items if item.id == recruiting.id)
    assert actor_item.actor is not None
    assert actor_item.actor.id == viewer.id
    assert actor_item.actor.display_name == viewer.display_name
    assert str(actor_item.actor.avatar_url) == viewer.avatar_url
    assert next(item for item in all_items if item.id == shipped.id).actor is None
    assert [item.id for item in (await page(h, viewer, kind=FeedKind.PROJECT_SHIPPED)).items] == [
        shipped.id
    ]
    assert {item.id for item in (await page(h, viewer, area="backend")).items} == {
        recruiting.id,
        new.id,
    }
    assert not (await page(h, viewer, kind=FeedKind.PROJECT_SHIPPED, area="backend")).items
    with pytest.raises(ProblemError) as error:
        await page(h, viewer, area="unknown")
    assert error.value.code == ErrorCode.NOT_FOUND


async def test_feed_project_visibility(harness: Harness) -> None:
    h = harness
    outsider = await h.user()
    participant = await h.user()
    admin = await h.admin()
    async with h.db() as session:
        public = await project(session)
        private = await project(session, visibility="members")
        member_row = member(session, private, participant, h.now)
        public_item = feed_item(session, FeedKind.PROJECT_RECRUITING, public.id, [], h.now)
        private_items = [
            feed_item(session, kind, private.id, [], h.now)
            for kind in (FeedKind.PROJECT_RECRUITING, FeedKind.PROJECT_SHIPPED)
        ]
    assert {item.id for item in (await page(h, outsider)).items} == {public_item.id}
    expected = {public_item.id, *(item.id for item in private_items)}
    assert {item.id for item in (await page(h, participant)).items} == expected
    assert {item.id for item in (await page(h, admin)).items} == expected
    async with h.db() as session:
        row = await session.get(models.ProjectMember, member_row.id)
        assert row is not None
        row.left_at = h.now
    assert {item.id for item in (await page(h, participant)).items} == {public_item.id}


async def test_feed_event_visibility_and_fresh_going_counts(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    dnd = await h.user(dnd_until=h.now + timedelta(hours=1))
    banned = await h.user(banned_at=h.now)
    indefinite = await h.user(dnd_indefinite=True)
    async with h.db() as session:
        session.add(models.Setting(key="attendance_claim_days", value=5, updated_by=viewer.id))
        current = await event(session, h.now)
        archived = await event(session, h.now, status="archived", days=-1)
        cancelled = await event(session, h.now, status="cancelled")
        old = await event(session, h.now, days=-6)
        silent = await event(session, h.now)
        attendance(session, current, viewer, h.now)
        attendance(session, current, dnd, h.now)
        attendance(session, current, banned, h.now)
        attendance(session, silent, indefinite, h.now)
        going = feed_item(session, FeedKind.EVENT_GOING, current.id, [], h.now)
        archived_item = feed_item(session, FeedKind.EVENT_NEW, archived.id, [], h.now)
        old_new = feed_item(session, FeedKind.EVENT_NEW, old.id, [], h.now)
        feed_item(session, FeedKind.EVENT_GOING, old.id, [], h.now)
        feed_item(session, FeedKind.EVENT_GOING, silent.id, [], h.now)
        feed_item(session, FeedKind.EVENT_GOING, cancelled.id, [], h.now)
        feed_item(session, FeedKind.EVENT_NEW, cancelled.id, [], h.now)
    result = await page(h, viewer)
    assert {item.id for item in result.items} == {going.id, archived_item.id, old_new.id}
    item = next(item for item in result.items if item.id == going.id)
    assert item.payload["count"] == 1
    assert item.payload["event_title"] == "Stored event title"
    async with h.db() as session:
        stored = await session.get(models.FeedItem, going.id)
        assert stored is not None
        assert stored.payload == going.payload
        user = await session.get(models.User, viewer.id)
        assert user is not None
        user.dnd_indefinite = True
    assert going.id not in {item.id for item in (await page(h, viewer)).items}


async def test_feed_help_and_mentorship_visibility(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    learner = await h.user()
    area = (await h.seed_areas(("backend",)))["backend"]
    async with h.db() as session:
        request = await help_request(session, learner, area, h.now)
        request.status = "cancelled"
        feed_item(session, FeedKind.HELP_REQUEST, request.id, [area], h.now, actor=learner)
        reached = await mentorship(session, viewer, learner, area, h.now)
        visible = feed_item(session, FeedKind.MENTORSHIP_COMPLETED, reached.id, [area], h.now)
        partial = await mentorship(session, viewer, learner, area, h.now, outcome="partial")
        feed_item(session, FeedKind.MENTORSHIP_COMPLETED, partial.id, [area], h.now)
    assert [item.id for item in (await page(h, viewer)).items] == [visible.id]
    async with h.db() as session:
        flag = models.Flag(
            user_id=viewer.id,
            subject_type="mentorship",
            subject_id=reached.id,
            related_user_ids=[learner.id],
            reason="Review",
            severity="low",
            status="to_check",
            algo_version="test",
        )
        session.add(flag)
    assert not (await page(h, viewer)).items
    async with h.db() as session:
        row = await session.get(models.Flag, flag.id)
        assert row is not None
        row.status = "pardoned"
    assert [item.id for item in (await page(h, viewer)).items] == [visible.id]


async def test_feed_mine_top_areas_and_mentor_help(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    learner = await h.user()
    areas = await h.seed_areas(("backend", "design", "data", "mobile", "devops"))
    async with h.db() as session:
        p = await project(session)
        items = {
            key: feed_item(session, FeedKind.PROJECT_SHIPPED, p.id, [area], h.now)
            for key, area in areas.items()
        }
        for key, score in (("backend", "9"), ("design", "8"), ("data", "7"), ("mobile", "6")):
            session.add(
                models.AreaScore(
                    user_id=viewer.id,
                    area_id=areas[key].id,
                    calculated=Decimal(score),
                    calc_reason="Score",
                )
            )
        session.add(
            models.AreaScore(
                user_id=viewer.id,
                area_id=areas["devops"].id,
                calculated=Decimal("10"),
                calc_reason="Score",
            )
        )
        archived = await session.get(models.Area, areas["devops"].id)
        assert archived is not None
        archived.archived_at = h.now
        session.add(
            models.MentorOffer(
                user_id=viewer.id, area_id=areas["mobile"].id, topics="Mobile", active=True
            )
        )
        request = await help_request(session, learner, areas["mobile"], h.now)
        extra = feed_item(
            session, FeedKind.HELP_REQUEST, request.id, [areas["mobile"]], h.now, actor=learner
        )
        own = await help_request(session, viewer, areas["mobile"], h.now)
        feed_item(session, FeedKind.HELP_REQUEST, own.id, [areas["mobile"]], h.now, actor=viewer)
    expected = {items[key].id for key in ("backend", "design", "data")}
    assert {item.id for item in (await page(h, viewer, mine_areas=True)).items} == expected | {
        extra.id
    }
    async with h.db() as session:
        score = await session.get(models.AreaScore, (viewer.id, areas["mobile"].id))
        assert score is not None
        score.override = Decimal("10")
    assert items["mobile"].id in {
        item.id for item in (await page(h, viewer, mine_areas=True)).items
    }
    assert items["data"].id not in {
        item.id for item in (await page(h, viewer, mine_areas=True)).items
    }


@pytest.mark.parametrize(
    "blocked", ["none", "dnd", "indefinite", "restriction", "capacity", "zero"]
)
async def test_feed_mentor_only_matching_and_availability(harness: Harness, blocked: str) -> None:
    h = harness
    viewer = await h.user(mentor_capacity=0 if blocked == "zero" else 1)
    learner = await h.user()
    area = (await h.seed_areas(("backend",)))["backend"]
    assert not (await page(h, viewer, mine_areas=True)).items
    async with h.db() as session:
        session.add(
            models.MentorOffer(user_id=viewer.id, area_id=area.id, topics="Backend", active=True)
        )
        request = await help_request(session, learner, area, h.now)
        item = feed_item(session, FeedKind.HELP_REQUEST, request.id, [area], h.now, actor=learner)
        p = await project(session)
        feed_item(session, FeedKind.PROJECT_SHIPPED, p.id, [area], h.now)
        user = await session.get(models.User, viewer.id)
        assert user is not None
        if blocked == "dnd":
            user.dnd_until = h.now + timedelta(hours=1)
        elif blocked == "indefinite":
            user.dnd_indefinite = True
        elif blocked == "restriction":
            session.add(
                models.MemberRestriction(
                    user_id=viewer.id,
                    kind="mentoring",
                    until=None,
                    reason="Restricted",
                    created_by=viewer.id,
                )
            )
        elif blocked == "capacity":
            await mentorship(session, viewer, learner, area, h.now, status="closing", outcome=None)
    async with h.db() as session:
        fresh_viewer = await session.get(models.User, viewer.id)
        assert fresh_viewer is not None
    result = await page(h, fresh_viewer, mine_areas=True)
    assert [entry.id for entry in result.items] == ([item.id] if blocked == "none" else [])
    if blocked in ("dnd", "restriction"):
        async with h.db() as session:
            user = await session.get(models.User, viewer.id)
            assert user is not None
            user.dnd_until = h.now
            for restriction in await session.scalars(select(models.MemberRestriction)):
                restriction.until = h.now
        async with h.db() as session:
            fresh_viewer = await session.get(models.User, viewer.id)
            assert fresh_viewer is not None
        assert [entry.id for entry in (await page(h, fresh_viewer, mine_areas=True)).items] == [
            item.id
        ]


async def test_feed_event_actions(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    other = await h.user()
    async with h.db() as session:
        e = await event(session, h.now)
        attendance(session, e, other, h.now)
        for kind in (FeedKind.EVENT_NEW, FeedKind.EVENT_GOING):
            feed_item(session, kind, e.id, [], h.now)
    assert all(item.allowed_actions == ["going"] for item in (await page(h, viewer)).items)
    async with h.db() as session:
        attendance_row = attendance(session, e, viewer, h.now)
        attendance_row.status = "attended"
    assert all(item.allowed_actions == [] for item in (await page(h, viewer)).items)
    h.advance(timedelta(days=1))
    assert all(item.allowed_actions == [] for item in (await page(h, other)).items)


async def test_feed_project_actions(harness: Harness) -> None:
    h = harness
    lead = await h.user()
    viewer = await h.user()
    participant = await h.user()
    pending = await h.user()
    async with h.db() as session:
        p = await project(session, lead_id=lead.id)
        member(session, p, participant, h.now)
        session.add(models.JoinRequest(project_id=p.id, user_id=pending.id, status="pending"))
        recruiting = feed_item(session, FeedKind.PROJECT_RECRUITING, p.id, [], h.now)
        feed_item(session, FeedKind.PROJECT_SHIPPED, p.id, [], h.now)
    result = await page(h, viewer)
    assert next(item for item in result.items if item.id == recruiting.id).allowed_actions == [
        "request_join"
    ]
    assert next(item for item in result.items if item.id != recruiting.id).allowed_actions == []
    for user in (lead, participant, pending):
        assert all(item.allowed_actions == [] for item in (await page(h, user)).items)
    async with h.db() as session:
        row = await session.get(models.Project, p.id)
        assert row is not None
        row.status = "shipped"
    assert recruiting.id not in {item.id for item in (await page(h, viewer)).items}


async def test_feed_help_actions(harness: Harness) -> None:
    h = harness
    learner = await h.user()
    viewer = await h.user()
    area = (await h.seed_areas(("backend",)))["backend"]
    async with h.db() as session:
        request = await help_request(session, learner, area, h.now)
        feed_item(session, FeedKind.HELP_REQUEST, request.id, [area], h.now, actor=learner)
    assert (await page(h, viewer)).items[0].allowed_actions == ["offer_help"]
    assert (await page(h, learner)).items[0].allowed_actions == []
    async with h.db() as session:
        session.add(
            models.HelpOffer(
                request_id=request.id, mentor_id=viewer.id, message="Help", status="withdrawn"
            )
        )
    assert (await page(h, viewer)).items[0].allowed_actions == []
    async with h.db() as session:
        row = await session.get(models.HelpRequest, request.id)
        assert row is not None
        row.status = "cancelled"
    assert not (await page(h, viewer)).items


async def test_feed_keyset_pagination_and_malformed_cursor(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    async with h.db() as session:
        p = await project(session)
        rows = [
            feed_item(session, FeedKind.PROJECT_SHIPPED, p.id, [], h.now + timedelta(seconds=index))
            for index in range(25)
        ]
    first = await page(h, viewer)
    assert [item.id for item in first.items] == [row.id for row in reversed(rows[5:])]
    assert first.next_cursor is not None
    second = await page(h, viewer, cursor=first.next_cursor)
    assert [item.id for item in second.items] == [row.id for row in reversed(rows[:5])]
    assert second.next_cursor is None
    assert not {item.id for item in first.items} & {item.id for item in second.items}
    with pytest.raises(ProblemError) as error:
        await page(h, viewer, cursor="malformed")
    assert error.value.code == ErrorCode.BAD_REQUEST


async def test_feed_dropped_going_items_advance_raw_cursor(harness: Harness) -> None:
    h = harness
    viewer = await h.user(dnd_indefinite=True)
    async with h.db() as session:
        e = await event(session, h.now)
        attendance(session, e, viewer, h.now)
        going = [
            feed_item(session, FeedKind.EVENT_GOING, e.id, [], h.now + timedelta(seconds=index))
            for index in (1, 2)
        ]
        new = feed_item(session, FeedKind.EVENT_NEW, e.id, [], h.now)
    first = await page(h, viewer, limit=2)
    assert first.items == []
    assert first.next_cursor is not None
    second = await page(h, viewer, limit=2, cursor=first.next_cursor)
    assert [item.id for item in second.items] == [new.id]
    assert second.next_cursor is None
    async with h.db() as session:
        assert len(list(await session.scalars(select(models.FeedItem)))) == len(going) + 1
