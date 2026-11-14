from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from harness import Harness
from hmm_contracts.algo import MentorshipRole
from hmm_contracts.common import (
    ActivityType,
    AreaImportance,
    AttendanceRole,
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    FactType,
    FlagStatus,
    MemberRole,
    ProjectStatus,
    Trust,
)
from hmm_core import models
from hmm_core.feed.confirmations import add_confirmation
from hmm_core.feed.history import list_pending_confirmations, list_user_facts
from hmm_core.feed.schemas import Fact, FactPage
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams


@contextmanager
def problem(code: ErrorCode) -> Generator[None]:
    with pytest.raises(ProblemError) as error:
        yield
    assert error.value.code == code


async def facts(
    h: Harness,
    viewer: models.User,
    target: models.User,
    *,
    fact_type: FactType | None = None,
    cursor: str | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> FactPage:
    async with h.db() as session:
        viewer_row = await session.get(models.User, viewer.id)
        assert viewer_row is not None
        return await list_user_facts(
            session,
            viewer_row,
            target.id,
            CursorParams(cursor=cursor, limit=limit),
            fact_type=fact_type,
            now=now or h.now,
        )


def by_id(page: FactPage, fact_id: UUID) -> Fact:
    return next(item for item in page.items if item.id == fact_id)


async def add_event(
    h: Harness,
    *,
    title: str = "Meetup",
    status: str = "published",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    description_md: str | None = "About",
) -> models.Event:
    async with h.db() as session:
        start = starts_at or h.now - timedelta(days=2)
        event = models.Event(
            slug=str(uuid4()),
            kind="meetup",
            formation_enabled=False,
            title=title,
            description_md=description_md,
            starts_at=start,
            ends_at=ends_at or start + timedelta(hours=3),
            timezone="Europe/Berlin",
            language=[],
            themes=[],
            status=status,
            review_reasons=[],
            origin="manual",
            unmapped_areas=[],
        )
        session.add(event)
        await session.flush()
        return event


async def attend(
    h: Harness, event: models.Event, user: models.User, **fields: Any
) -> models.EventAttendance:
    async with h.db() as session:
        row = models.EventAttendance(
            event_id=event.id,
            user_id=user.id,
            status=fields.pop("status", "attended"),
            role=fields.pop("role", "attendee"),
            proof_links=fields.pop("proof_links", []),
            trust=fields.pop("trust", "self"),
            status_at=h.now,
            **fields,
        )
        session.add(row)
        await session.flush()
        return row


async def add_project(
    h: Harness, *, name: str = "Project", visibility: str = "public", **fields: Any
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            name=name,
            description_md=fields.pop("description_md", f"{name} description"),
            goal="Goal",
            links=fields.pop("links", []),
            visibility=visibility,
            status=fields.pop("status", "active"),
            origin="standalone",
            **fields,
        )
        session.add(project)
        await session.flush()
        return project


async def join(
    h: Harness, project: models.Project, user: models.User, **fields: Any
) -> models.ProjectMember:
    async with h.db() as session:
        row = models.ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=fields.pop("role", "member"),
            state=fields.pop("state", "confirmed"),
            trust=fields.pop("trust", "self"),
            joined_via="invite",
            is_team_row=False,
            joined_at=fields.pop("joined_at", h.now - timedelta(days=5)),
            **fields,
        )
        session.add(row)
        await session.flush()
        return row


async def add_mentorship(
    h: Harness,
    mentor: models.User,
    learner: models.User,
    area_id: UUID,
    *,
    status: str = "closed",
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
            kind="mentoring",
            mentor_score_at_start=Decimal("6"),
            learner_score_at_start=Decimal("3"),
            status=status,
            started_at=h.now - timedelta(days=30),
            closed_at=h.now - timedelta(days=1) if status == "closed" else None,
            outcome="reached" if status == "closed" else None,
            mentor_trust="self",
            learner_trust="self",
        )
        session.add(row)
        await session.flush()
        return row


async def add_entry(h: Harness, user: models.User, **fields: Any) -> models.ActivityEntry:
    async with h.db() as session:
        row = models.ActivityEntry(
            user_id=user.id,
            type=fields.pop("type", "course"),
            title=fields.pop("title", "Course"),
            description_md=fields.pop("description_md", "Finished"),
            links=fields.pop("links", []),
            occurred_on=fields.pop("occurred_on", (h.now - timedelta(days=3)).date()),
            **fields,
        )
        session.add(row)
        await session.flush()
        return row


async def add_flag(
    h: Harness,
    user: models.User,
    subject_type: str,
    subject_id: UUID,
    status: str,
    penalty: dict[str, bool] | None = None,
) -> None:
    async with h.db() as session:
        session.add(
            models.Flag(
                user_id=user.id,
                subject_type=subject_type,
                subject_id=subject_id,
                related_user_ids=[],
                reason="Review",
                severity="medium",
                status=status,
                algo_version="test",
                penalty=penalty,
            )
        )


async def confirm(
    h: Harness,
    fact_type: ConfirmationFactType,
    fact_id: UUID,
    subject: models.User,
    confirmer: models.User,
    now: datetime | None = None,
) -> models.Confirmation:
    async with h.db() as session:
        return await add_confirmation(
            session,
            h.jobs,
            fact_type=fact_type,
            fact_id=fact_id,
            subject_user_id=subject.id,
            confirmer_id=confirmer.id,
            source=ConfirmationSource.PEER,
            now=now or h.now,
        )


async def test_attendance_fact_shape(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    event = await add_event(h, title="Data meetup", description_md="Talks")
    async with h.db() as session:
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
                    headcount=None,
                ),
            ]
        )
    links = ["https://proof.test/1", "https://proof.test/2"]
    row = await attend(h, event, member, role="speaker", trust="peer", proof_links=links)
    for status in ("cancelled", "draft"):
        await attend(h, await add_event(h, status=status), member)
    page = await facts(h, member, member)
    assert [item.id for item in page.items] == [row.id]
    fact = page.items[0]
    assert fact.type == FactType.ATTENDANCE
    assert fact.user_id == member.id
    assert fact.subject_type == "event"
    assert fact.subject_id == event.id
    assert fact.title == "Data meetup"
    assert fact.description_md == "Talks"
    assert fact.links == []
    assert fact.activity_type is None
    assert fact.occurred_on == event.starts_at.date()
    assert fact.event is not None
    assert fact.event.id == event.id
    assert fact.event.title == "Data meetup"
    assert fact.event.timezone == "Europe/Berlin"
    assert fact.event.starts_at == event.starts_at
    assert fact.event.ends_at == event.ends_at
    assert fact.project is None
    assert fact.role == AttendanceRole.SPEAKER
    assert fact.trust == Trust.PEER
    assert [str(link) for link in fact.proof_links] == links
    assert fact.area is not None
    assert (fact.area.key, fact.area.name) == ("backend", "Backend")
    assert fact.note is None
    assert fact.award is None
    assert fact.confirmations == []
    assert fact.allowed_actions == []
    assert page.next_cursor is None


async def test_project_fact_shape_filter_and_visibility(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    lead = await h.user()
    outsider = await h.user()
    teammate = await h.user()
    admin = await h.admin()
    project = await add_project(
        h,
        name="Shipped tool",
        status="shipped",
        links=["https://repo.test/tool"],
        lead_user_id=lead.id,
    )
    async with h.db() as session:
        session.add(models.ProjectArea(project_id=project.id, area_id=areas["backend"].id))
        session.add(
            models.ProjectRelease(
                project_id=project.id, title="v1", notes_md="First", created_by=lead.id
            )
        )
    row = await join(h, project, member, area_id=areas["backend"].id)
    await join(h, project, lead, role="lead")
    async with h.db() as session:
        session.add(
            models.FactAward(
                fact_type=FactType.PROJECT_MEMBER,
                fact_id=row.id,
                user_id=member.id,
                xp=Decimal("12.5"),
                activity=Decimal("3.25"),
                reason="Shipped",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )
    hidden = await add_project(h, name="Private", visibility="members")
    hidden_row = await join(h, hidden, member)
    await join(h, hidden, teammate)
    attendance = await attend(h, await add_event(h), member)

    page = await facts(h, outsider, member)
    assert {item.id for item in page.items} == {row.id, attendance.id}
    fact = by_id(page, row.id)
    assert fact.type == FactType.PROJECT_MEMBER
    assert fact.subject_type == "project"
    assert fact.subject_id == project.id
    assert fact.title == "Shipped tool"
    assert fact.description_md == "Shipped tool description"
    assert [str(link) for link in fact.links] == ["https://repo.test/tool"]
    assert fact.occurred_on == row.joined_at.date()
    assert fact.project is not None
    assert fact.project.id == project.id
    assert fact.project.name == "Shipped tool"
    assert fact.project.event_id is None
    assert fact.event is None
    assert fact.role == MemberRole.MEMBER
    assert fact.outcome == ProjectStatus.SHIPPED
    assert fact.area is not None
    assert fact.area.key == "backend"
    assert fact.award is not None
    assert fact.award.fact_type == FactType.PROJECT_MEMBER
    assert fact.award.fact_id == row.id
    assert fact.award.user_id == member.id
    assert fact.award.xp == 12.5
    assert fact.award.activity == 3.25
    assert isinstance(fact.award.xp, float)
    assert fact.award.reason == "Shipped"
    assert fact.award.algo_version == "fake-1"

    filtered = await facts(h, outsider, member, fact_type=FactType.PROJECT_MEMBER)
    assert [item.id for item in filtered.items] == [row.id]
    for viewer in (teammate, admin):
        visible = await facts(h, viewer, member, fact_type=FactType.PROJECT_MEMBER)
        assert {item.id for item in visible.items} == {row.id, hidden_row.id}


async def test_mentorship_fact_shape(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    mentor = await h.user()
    learner = await h.user()
    mentorship = await add_mentorship(h, mentor, learner, areas["data"].id)
    await add_mentorship(h, mentor, learner, areas["design"].id, status="cancelled")
    for user, role in ((mentor, MentorshipRole.MENTOR), (learner, MentorshipRole.MENTEE)):
        page = await facts(h, user, user)
        assert [item.id for item in page.items] == [mentorship.id]
        fact = page.items[0]
        assert fact.type == FactType.MENTORSHIP_SIDE
        assert fact.subject_type == "mentorship"
        assert fact.subject_id == mentorship.id
        assert fact.title == "Mentorship in data"
        assert fact.role == role
        assert fact.description_md is None
        assert fact.links == []
        assert fact.event is None
        assert fact.project is None
        assert fact.occurred_on == mentorship.started_at.date()
        assert fact.area is not None
        assert fact.area.key == "data"


async def test_activity_entries_and_flags(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    member = await h.user()
    entry = await add_entry(
        h,
        member,
        type="talk",
        title="Conference talk",
        description_md="Slides",
        links=["https://talk.test/slides"],
        area_id=areas["ml_ai"].id,
    )
    await add_entry(h, member, title="Deleted", deleted_at=h.now)
    punished = await add_entry(h, member, title="Punished")
    pardoned = await add_entry(h, member, title="Pardoned")
    flagged = await attend(h, await add_event(h), member)
    await add_flag(h, member, "attendance", flagged.id, "to_check")
    await add_flag(h, member, "activity_entry", punished.id, "punished", {"exclude": True})
    await add_flag(h, member, "activity_entry", pardoned.id, "pardoned")
    page = await facts(h, member, member)
    assert {item.id for item in page.items} == {entry.id, pardoned.id}
    fact = by_id(page, entry.id)
    assert fact.type == FactType.ACTIVITY_ENTRY
    assert fact.subject_type == "activity_entry"
    assert fact.subject_id == entry.id
    assert fact.title == "Conference talk"
    assert fact.description_md == "Slides"
    assert [str(link) for link in fact.links] == ["https://talk.test/slides"]
    assert fact.activity_type == ActivityType.TALK
    assert fact.occurred_on == entry.occurred_on
    assert fact.confirmations == []
    assert fact.event is None
    assert fact.project is None
    assert fact.trust == Trust.SELF
    assert fact.area is not None
    assert fact.area.key == "ml_ai"
    assert by_id(page, pardoned.id).flag_status == FlagStatus.PARDONED


async def test_confirm_and_withdraw_actions(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    target = await h.user()
    event = await add_event(h, ends_at=h.now - timedelta(hours=1))
    await attend(h, event, viewer)
    fact = await attend(h, event, target)
    page = await facts(h, viewer, target)
    assert [item.allowed_actions for item in page.items] == [["confirm"]]
    assert (await facts(h, viewer, viewer)).items[0].allowed_actions == []
    confirmation = await confirm(h, ConfirmationFactType.ATTENDANCE, fact.id, target, viewer)
    page = await facts(h, viewer, target)
    item = page.items[0]
    assert item.allowed_actions == []
    assert [row.id for row in item.confirmations] == [confirmation.id]
    view = item.confirmations[0]
    assert view.fact_type == ConfirmationFactType.ATTENDANCE
    assert view.fact_id == fact.id
    assert view.subject_user_id == target.id
    assert view.confirmer_id == viewer.id
    assert view.source == ConfirmationSource.PEER
    assert view.allowed_actions == ["withdraw"]
    assert (await facts(h, target, target)).items[0].confirmations[0].allowed_actions == []
    later = await facts(h, viewer, target, now=h.now + timedelta(days=8))
    assert later.items[0].confirmations[0].allowed_actions == []


async def test_pagination(harness: Harness) -> None:
    h = harness
    member = await h.user()
    today = h.now.date()
    newest = [
        await add_entry(h, member, title=f"New {index}", occurred_on=today - timedelta(days=1))
        for index in range(2)
    ]
    oldest = await add_entry(h, member, title="Old", occurred_on=today - timedelta(days=5))
    await attend(h, await add_event(h, starts_at=h.now - timedelta(hours=6)), member)

    first = await facts(h, member, member, fact_type=FactType.ACTIVITY_ENTRY, limit=2)
    assert [item.id for item in first.items] == sorted(
        (row.id for row in newest), key=str, reverse=True
    )
    assert first.next_cursor is not None
    second = await facts(
        h, member, member, fact_type=FactType.ACTIVITY_ENTRY, limit=2, cursor=first.next_cursor
    )
    assert [item.id for item in second.items] == [oldest.id]
    assert second.next_cursor is None

    everything = await facts(h, member, member, limit=2)
    assert [item.type for item in everything.items] == [
        FactType.ATTENDANCE,
        FactType.ACTIVITY_ENTRY,
    ]
    with problem(ErrorCode.BAD_REQUEST):
        await facts(h, member, member, cursor="not-a-cursor")


async def test_target_gate(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    pending = await h.user(onboarded=False)
    with problem(ErrorCode.NOT_FOUND):
        await facts(h, viewer, pending)
    async with h.db() as session:
        viewer_row = await session.get(models.User, viewer.id)
        assert viewer_row is not None
        with problem(ErrorCode.NOT_FOUND):
            await list_user_facts(
                session,
                viewer_row,
                uuid4(),
                CursorParams(cursor=None, limit=20),
                fact_type=None,
                now=h.now,
            )


async def test_pending_confirmation_groups(harness: Harness) -> None:
    h = harness
    viewer = await h.user()
    async with h.db() as session:
        viewer_row = await session.get(models.User, viewer.id)
        assert viewer_row is not None
        assert await list_pending_confirmations(session, viewer_row, h.now) == []

    first, second, confirmed, going, flagged, banned = [await h.user() for _ in range(6)]
    teammate, stranger = await h.user(), await h.user()
    event = await add_event(h, title="Ended", ends_at=h.now - timedelta(hours=1))
    await attend(h, event, viewer)
    first_row = await attend(h, event, first)
    second_row = await attend(h, event, second)
    confirmed_row = await attend(h, event, confirmed)
    await attend(h, event, going, status="going")
    flagged_row = await attend(h, event, flagged)
    await attend(h, event, banned)
    await add_flag(h, flagged, "attendance", flagged_row.id, "to_check")
    await confirm(h, ConfirmationFactType.ATTENDANCE, confirmed_row.id, confirmed, viewer)
    async with h.db() as session:
        banned_row = await session.get(models.User, banned.id)
        assert banned_row is not None
        banned_row.banned_at = h.now
    upcoming = await add_event(h, title="Upcoming", ends_at=h.now + timedelta(hours=2))
    await attend(h, upcoming, viewer)
    await attend(h, upcoming, first)
    project = await add_project(h, name="Team")
    await join(h, project, viewer)
    member_row = await join(h, project, teammate)
    other = await add_project(h, name="Other")
    await join(h, other, stranger)

    async with h.db() as session:
        viewer_row = await session.get(models.User, viewer.id)
        assert viewer_row is not None
        groups = await list_pending_confirmations(session, viewer_row, h.now)
    assert [(group.subject_type, group.subject_id, group.title) for group in groups] == [
        ("event", event.id, "Ended"),
        ("project", project.id, "Team"),
    ]
    event_group, project_group = groups
    assert [item.id for item in event_group.facts] == sorted([first_row.id, second_row.id], key=str)
    assert {item.user_id for item in event_group.facts} == {first.id, second.id}
    assert [item.id for item in project_group.facts] == [member_row.id]
    assert project_group.facts[0].user_id == teammate.id
    for group in groups:
        for item in group.facts:
            assert item.allowed_actions == ["confirm"]
