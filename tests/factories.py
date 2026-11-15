import itertools
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from harness import NOW, Harness
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import FormationPlan, PlannedTeam
from hmm.catalog import events as catalog_events
from hmm.common import (
    AreaImportance,
    EnrollmentSource,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    QuizStatus,
)
from hmm.formation import tick
from hmm.platform.errors import ProblemError

ROW_DEFAULTS: dict[type[models.Base], dict[str, Any]] = {
    models.Event: {
        "kind": "hackathon",
        "formation_enabled": False,
        "timezone": "UTC",
        "language": [],
        "themes": [],
        "status": "published",
        "review_reasons": [],
        "origin": "manual",
        "unmapped_areas": [],
    },
    models.ProjectMember: {"state": "confirmed", "trust": "self"},
    models.HelpRequest: {"format": "online", "status": "matched"},
    models.Mentorship: {"kind": "mentoring", "mentor_trust": "self", "learner_trust": "self"},
    models.EventAttendance: {"proof_links": []},
    models.Flag: {"algo_version": "test", "related_user_ids": [], "severity": "medium"},
}


def new_row[M: models.Base](model: type[M], **fields: Any) -> M:
    return model(**{**deepcopy(ROW_DEFAULTS.get(model, {})), **fields})


_titles = itertools.count(1)

AREAS = (("backend", AreaImportance.PRIMARY), ("frontend", AreaImportance.NICE))


async def seed_areas(h: Harness) -> None:
    async with h.db() as session:
        if await session.scalar(select(models.Area.id).limit(1)) is None:
            await h.seed_areas()


async def make_event(
    h: Harness,
    *,
    minimum: int = 3,
    maximum: int = 5,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    status: EventStatus = EventStatus.PUBLISHED,
) -> models.Event:
    await seed_areas(h)
    async with h.db() as session:
        return await catalog_events.create_event(
            session,
            catalog_events.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=minimum,
                team_size_max=maximum,
                formation_enabled=True,
                areas=[
                    catalog_events.EventAreaInput(key=key, importance=importance)
                    for key, importance in AREAS
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=None,
            now=h.now,
        )


async def join(h: Harness, event: models.Event, user: models.User, **body: Any) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/events/{event.id}/enroll", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def formation(h: Harness, event: models.Event, user: models.User) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.get(f"/api/v1/events/{event.id}/formation")
    assert response.status_code == 200, response.text
    return response.json()


async def create_party(h: Harness, event: models.Event, user: models.User) -> dict[str, Any]:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/events/{event.id}/party")
    assert response.status_code == 201, response.text
    return response.json()


async def invite_to_party(
    h: Harness, party_id: UUID, actor: models.User, user: models.User
) -> dict[str, Any]:
    async with await h.client(actor) as client:
        response = await client.post(
            f"/api/v1/parties/{party_id}/invites", json={"user_id": str(user.id)}
        )
    assert response.status_code == 201, response.text
    return response.json()


async def accept_invite(h: Harness, user: models.User, invite_id: UUID) -> None:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/invites/{invite_id}/accept")
    assert response.status_code == 200, response.text


async def seed_enrollment(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "active",
    status_at: datetime | None = None,
    party_id: UUID | None = None,
    willing_to_lead: bool = False,
) -> models.Enrollment:
    async with h.db() as session:
        row = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            status_at=status_at or h.now,
            party_id=party_id,
            willing_to_lead=willing_to_lead,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def seed_team(
    h: Harness,
    event: models.Event,
    lead: models.User,
    members: tuple[models.User, ...] = (),
    *,
    state: str = "confirmed",
    accept_by: datetime | None = None,
    seats_closed: bool = False,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=f"Team {h.now:%H%M%S%f}",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="idea",
            team_status="formed",
            origin="formation",
            lead_user_id=lead.id,
            seats_closed_at=h.now if seats_closed else None,
        )
        session.add(project)
        await session.flush()
        for user in (lead, *members):
            session.add(
                new_row(
                    models.ProjectMember,
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role="lead" if user.id == lead.id else "member",
                    state=state,
                    joined_via="formation",
                    accept_by=None
                    if state == "confirmed"
                    else accept_by or h.now + timedelta(days=1),
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now if state == "confirmed" else None,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


async def formation_row(h: Harness, event: models.Event) -> models.EventFormation:
    async with h.db() as session:
        return await session.get_one(models.EventFormation, event.id)


async def set_phase(h: Harness, event: models.Event, phase: str) -> None:
    async with h.db() as session:
        row = await session.get_one(models.EventFormation, event.id)
        row.phase = phase
        await session.flush()


async def maybe_enrollment(
    h: Harness, event: models.Event, user: models.User
) -> models.Enrollment | None:
    async with h.db() as session:
        return await session.scalar(
            select(models.Enrollment)
            .where(models.Enrollment.event_id == event.id, models.Enrollment.user_id == user.id)
            .order_by(models.Enrollment.status_at.desc(), models.Enrollment.id.desc())
            .limit(1)
        )


async def enrollment_row(h: Harness, event: models.Event, user: models.User) -> models.Enrollment:
    row = await maybe_enrollment(h, event, user)
    assert row is not None
    return row


async def queue_rows(h: Harness, event: models.Event) -> list[models.Enrollment]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Enrollment)
                .where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.status.in_(["active", "reserve"]),
                )
                .order_by(models.Enrollment.status_at, models.Enrollment.id)
            )
        )


async def parties(h: Harness, event: models.Event) -> list[models.Party]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Party)
                .where(models.Party.event_id == event.id)
                .order_by(models.Party.created_at, models.Party.id)
            )
        )


async def invites(h: Harness, event: models.Event) -> list[models.Invite]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Invite)
                .where(models.Invite.event_id == event.id)
                .order_by(models.Invite.created_at, models.Invite.id)
            )
        )


async def teams(h: Harness, event: models.Event) -> list[models.Project]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.Project)
                .where(models.Project.event_id == event.id, models.Project.team_status.is_not(None))
                .order_by(models.Project.created_at, models.Project.id)
            )
        )


async def live_members(h: Harness, project: models.Project) -> list[models.ProjectMember]:
    async with h.db() as session:
        return list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )


async def stored_member(
    h: Harness, project: models.Project, user: models.User
) -> models.ProjectMember:
    async with h.db() as session:
        return (
            await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.user_id == user.id,
                )
            )
        ).one()


async def notices(
    h: Harness, *, user: models.User | None = None, kind: str | None = None
) -> list[models.Notification]:
    async with h.db() as session:
        query = select(models.Notification).order_by(
            models.Notification.created_at, models.Notification.id
        )
        if user is not None:
            query = query.where(models.Notification.user_id == user.id)
        if kind is not None:
            query = query.where(models.Notification.type == kind)
        return list(await session.scalars(query))


async def alerts(h: Harness, kind: str | None = None) -> list[models.AdminAlert]:
    async with h.db() as session:
        query = select(models.AdminAlert).order_by(models.AdminAlert.created_at)
        if kind is not None:
            query = query.where(models.AdminAlert.type == kind)
        return list(await session.scalars(query))


async def run_tick(h: Harness, event: models.Event) -> None:
    async with h.sessionmaker.begin() as session:
        await tick.tick_event(session, h.runtime, event.id, h.now)


async def form_now(h: Harness, event: models.Event, admin: models.User) -> dict[str, Any]:
    async with await h.client(admin) as client:
        response = await client.post(f"/api/v1/admin/events/{event.id}/formation/run")
    assert response.status_code == 202, response.text
    await h.run_jobs()
    return response.json()


async def formed(
    h: Harness,
    admin: models.User,
    *,
    count: int = 3,
    minimum: int = 3,
    maximum: int = 5,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
) -> tuple[models.Event, list[models.User], models.Project]:
    event = await make_event(
        h,
        minimum=minimum,
        maximum=maximum,
        starts_in=starts_in,
        registration_deadline=registration_deadline,
    )
    users = [await h.user() for _ in range(count)]
    for user in users:
        await join(h, event, user)
        h.advance(timedelta(seconds=1))
    await form_now(h, event, admin)
    [team] = await teams(h, event)
    return event, users, team


async def confirm(h: Harness, user: models.User, team: models.Project) -> None:
    async with await h.client(user) as client:
        response = await client.post(f"/api/v1/projects/{team.id}/placement/accept")
    assert response.status_code == 200, response.text


async def runs(h: Harness, event: models.Event, admin: models.User) -> list[dict[str, Any]]:
    async with await h.client(admin) as client:
        response = await client.get(f"/api/v1/admin/events/{event.id}/formation/runs")
    assert response.status_code == 200, response.text
    return response.json()


def payload(notification: models.Notification) -> dict[str, Any]:
    assert isinstance(notification.payload, dict)
    return notification.payload


def plan(*teams: list[UUID]) -> FormationPlan:
    return FormationPlan(
        teams=[PlannedTeam(units=list(units), reason="test plan") for units in teams],
        summary="test plan",
    )


async def count(h: Harness, model: Any, *where: Any) -> int:
    async with h.db() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


@contextmanager
def problem(code: ErrorCode) -> Generator[None]:
    with pytest.raises(ProblemError) as error:
        yield
    assert error.value.code == code


async def add_active_mentorship(
    h: Harness, learner: models.User, mentor: models.User, area: models.Area
) -> None:
    async with h.db() as session:
        request = new_row(
            models.HelpRequest,
            learner_id=learner.id,
            area_id=area.id,
            topic="Past",
            goal="Past goal",
            expires_at=h.now + timedelta(days=14),
        )
        session.add(request)
        await session.flush()
        session.add(
            new_row(
                models.Mentorship,
                request_id=request.id,
                mentor_id=mentor.id,
                learner_id=learner.id,
                area_id=area.id,
                mentor_score_at_start=Decimal("7.00"),
                learner_score_at_start=Decimal("2.00"),
                status="active",
                started_at=h.now,
            )
        )


async def mentorship_row(h: Harness, mentorship_id: UUID) -> models.Mentorship:
    async with h.db() as session:
        row = await session.get(models.Mentorship, mentorship_id)
        assert row is not None
        return row


async def add_offers(h: Harness, request_id: str, *mentors: models.User) -> list[UUID]:
    async with h.db() as session:
        offers = [
            models.HelpOffer(
                request_id=UUID(request_id),
                mentor_id=mentor.id,
                message=f"I can help {mentor.display_name}",
                status="pending",
            )
            for mentor in mentors
        ]
        session.add_all(offers)
        await session.flush()
        return [offer.id for offer in offers]


REQUESTS = "/api/v1/help-requests"


def request_body(area_key: str = "backend", **fields: Any) -> dict[str, Any]:
    return {
        "area_key": area_key,
        "topic": "FastAPI auth",
        "goal": "ship a login flow",
        "format": "online",
        **fields,
    }


async def create_request(
    c: httpx.AsyncClient, area_key: str = "backend", **fields: Any
) -> dict[str, Any]:
    response = await c.post(REQUESTS, json=request_body(area_key, **fields))
    assert response.status_code == 201, response.text
    return response.json()


async def enroll(
    session: AsyncSession,
    event: models.Event,
    user: models.User,
    status: str = "active",
    **fields: Any,
) -> models.Enrollment:
    enrollment = models.Enrollment(
        event_id=event.id,
        user_id=user.id,
        status=status,
        source=EnrollmentSource.SELF.value,
        status_at=NOW,
        **fields,
    )
    session.add(enrollment)
    await session.flush()
    await session.refresh(enrollment)
    return enrollment


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    areas: tuple[tuple[str, AreaImportance], ...] = (("backend", AreaImportance.PRIMARY),),
) -> models.Event:
    async with h.db() as session:
        return await catalog_events.create_event(
            session,
            catalog_events.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Rec Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=None,
                areas=[
                    catalog_events.EventAreaInput(key=key, importance=importance)
                    for key, importance in areas
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )


QUESTIONS: list[dict[str, Any]] = [
    {
        "key": "languages",
        "type": "multi_choice",
        "section": "general",
        "required": True,
        "visible_if": None,
        "config": {"min": 1, "max": 2, "allow_other": True},
        "options": [("python", {"backend": 1.0}), ("react", {"frontend": 0.8})],
    },
    {
        "key": "years",
        "type": "number",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {"min": 0, "max": 50, "unit": "years"},
        "options": [],
    },
    {
        "key": "direction",
        "type": "single_choice",
        "section": "general",
        "required": True,
        "visible_if": {"q": "languages", "op": "includes", "value": "python"},
        "config": {"allow_other": False},
        "options": [("backend", {"backend": 1.0}), ("frontend", {"frontend": 1.0})],
    },
    {
        "key": "about",
        "type": "text",
        "section": "advanced",
        "required": False,
        "visible_if": {"not": {"q": "years", "op": "gte", "value": 2}},
        "config": {"max_length": 20, "multiline": False},
        "options": [],
    },
    {
        "key": "rating",
        "type": "scale",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {"min": 1, "max": 10, "min_label": "new", "max_label": "expert"},
        "options": [],
    },
    {
        "key": "link",
        "type": "url",
        "section": "general",
        "required": False,
        "visible_if": None,
        "config": {},
        "options": [],
    },
]


async def archive_published_quizzes(session: Any) -> None:
    await session.execute(
        update(models.Quiz)
        .where(models.Quiz.status == QuizStatus.PUBLISHED)
        .values(status=QuizStatus.ARCHIVED)
    )
    await session.flush()


async def next_quiz_version(session: Any) -> int:
    return (await session.scalar(select(func.coalesce(func.max(models.Quiz.version), 0)))) + 1


async def seed_quiz(h: Harness, admin: models.User, *, drop: str | None = None) -> models.Quiz:
    async with h.db() as session:
        await archive_published_quizzes(session)
        quiz = models.Quiz(
            version=await next_quiz_version(session),
            title="Onboarding",
            status=QuizStatus.PUBLISHED,
            created_by=admin.id,
            published_at=h.now,
        )
        session.add(quiz)
        await session.flush()
        session.add_all(
            [
                models.QuizSectionDef(quiz_id=quiz.id, key="general", position=0, visible_if=None),
                models.QuizSectionDef(
                    quiz_id=quiz.id,
                    key="advanced",
                    position=1,
                    visible_if={
                        "any": [
                            {"q": "languages", "op": "includes", "value": "python"},
                            {"q": "years", "op": "gte", "value": 2},
                        ]
                    },
                ),
            ]
        )
        rows: dict[str, models.Question] = {}
        for position, spec in enumerate(QUESTIONS):
            if spec["key"] == drop:
                continue
            rows[spec["key"]] = models.Question(
                quiz_id=quiz.id,
                section=spec["section"],
                key=spec["key"],
                type=spec["type"],
                text=spec["key"].title(),
                help=None,
                required=spec["required"],
                position=position,
                visible_if=spec["visible_if"],
                config=spec["config"],
            )
        session.add_all(rows.values())
        await session.flush()
        session.add_all(
            models.QuestionOption(
                question_id=rows[spec["key"]].id,
                key=key,
                label=key.title(),
                position=index,
                area_hints=hints,
            )
            for spec in QUESTIONS
            if spec["key"] in rows
            for index, (key, hints) in enumerate(spec["options"])
        )
        await session.flush()
        await session.refresh(quiz)
        return quiz


async def onboarding_setup(h: Harness) -> tuple[models.User, models.Quiz, models.User]:
    await h.seed_areas(("backend", "frontend"))
    admin = await h.admin()
    quiz = await seed_quiz(h, admin)
    member = await h.user(onboarded=False)
    return admin, quiz, member


async def onboarding_status(h: Harness, user_id: UUID) -> str:
    async with h.db() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        return user.onboarding_status


async def seed_project(
    h: Harness,
    lead: models.User,
    *,
    members: Sequence[models.User] = (),
    name: str = "Project",
    visibility: str = "public",
    status: str = "idea",
    event_id: Any = None,
    area_keys: Sequence[str] = (),
    recruiting: dict[str, Any] | None = None,
    team_status: str | None = None,
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event_id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility=visibility,
            recruiting=recruiting,
            lead_user_id=lead.id,
            status=status,
            team_status=team_status,
            origin="standalone" if team_status is None else "formation",
            last_progress_at=h.now,
        )
        session.add(project)
        await session.flush()
        for key in area_keys:
            area_id = await session.scalar(select(models.Area.id).where(models.Area.key == key))
            assert area_id is not None
            session.add(models.ProjectArea(project_id=project.id, area_id=area_id))
        for user in (lead, *members):
            is_lead = user.id == lead.id
            session.add(
                new_row(
                    models.ProjectMember,
                    project_id=project.id,
                    event_id=event_id if team_status is not None else None,
                    user_id=user.id,
                    role="lead" if is_lead else "member",
                    joined_via="create",
                    is_team_row=False,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project
