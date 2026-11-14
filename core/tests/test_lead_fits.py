import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from harness import NOW, Harness
from hmm_contracts.algo import LeadFit as AlgoLeadFit
from hmm_contracts.common import (
    AreaImportance,
    EnrollmentSource,
    EnrollmentStatus,
    ErrorCode,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationPhase,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
)
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service
from hmm_core.formation.jobs import compute_lead_fits, compute_lead_fits_for_member
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_api_skeleton import assert_problem

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    areas: tuple[tuple[str, AreaImportance], ...] = (("backend", AreaImportance.PRIMARY),),
    kind: EventKind = EventKind.HACKATHON,
    status: EventStatus = EventStatus.PUBLISHED,
) -> models.Event:
    async with h.db() as session:
        return await catalog.create_event(
            session,
            catalog.EventInput(
                kind=kind,
                title=f"Lead Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                areas=[
                    catalog.EventAreaInput(key=key, importance=importance)
                    for key, importance in areas
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=status,
            created_by=None,
            now=h.now,
        )


async def set_phase(h: Harness, event: models.Event, phase: FormationPhase) -> None:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value)
        )


async def join_team(
    session: AsyncSession,
    event: models.Event,
    user: models.User,
    *,
    team_status: str = "forming",
) -> models.Project:
    project = models.Project(
        event_id=event.id,
        name="Core",
        description_md="",
        goal="",
        links=[],
        visibility=ProjectVisibility.PUBLIC.value,
        status=ProjectStatus.IDEA.value,
        team_status=team_status,
        origin=ProjectOrigin.CLAIM.value,
        lead_user_id=user.id,
    )
    session.add(project)
    await session.flush()
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=event.id,
            user_id=user.id,
            role="lead",
            state="confirmed",
            trust="self",
            joined_via="claim",
            is_team_row=True,
            joined_at=NOW,
        )
    )
    await session.flush()
    return project


async def fits_of(h: Harness, event: models.Event) -> dict[object, models.LeadFit]:
    async with h.db() as session:
        return {
            fit.user_id: fit
            for fit in await session.scalars(
                select(models.LeadFit).where(models.LeadFit.event_id == event.id)
            )
        }


async def add_fit(
    h: Harness, event: models.Event, user: models.User, fit: str, reason: str = "x"
) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(fit),
                reason=reason,
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def test_candidates_and_fit_persistence(harness: Harness) -> None:
    h = harness
    areas = await h.seed_areas()
    event = await make_hackathon(h)
    strong = await h.user()
    weak = await h.user()
    newbie = await h.user(onboarded=False)
    banned = await h.user(banned_at=h.now)
    away = await h.user(dnd_indefinite=True)
    later = await h.user(dnd_until=h.now + timedelta(days=1))
    teammate = await h.user()
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=strong.id,
                area_id=areas["backend"].id,
                calculated=Decimal("10"),
                calc_reason="x",
            )
        )
        await join_team(session, event, teammate)
    await compute_lead_fits(h.runtime, event_id=str(event.id))
    fits = await fits_of(h, event)
    assert set(fits) == {strong.id, weak.id}
    assert float(fits[strong.id].fit) == 0.6
    assert float(fits[weak.id].fit) == 0.0
    for fit in fits.values():
        assert fit.algo_version == "fake-1"
        assert fit.computed_at == h.now
        assert fit.reason
    for user in (newbie, banned, away, later, teammate):
        assert user.id not in fits


async def test_compute_is_idempotent(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await h.user()
    await h.user()
    await compute_lead_fits(h.runtime, event_id=str(event.id))
    first = {
        user_id: (fit.fit, fit.reason, fit.algo_version, fit.computed_at, fit.created_at)
        for user_id, fit in (await fits_of(h, event)).items()
    }
    await compute_lead_fits(h.runtime, event_id=str(event.id))
    second = {
        user_id: (fit.fit, fit.reason, fit.algo_version, fit.computed_at, fit.created_at)
        for user_id, fit in (await fits_of(h, event)).items()
    }
    assert len(first) == 2
    assert first == second


async def test_cron_fan_out_targets_open_and_locked_events(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    opened = await make_hackathon(h)
    locked = await make_hackathon(h)
    review = await make_hackathon(h)
    closed = await make_hackathon(h)
    await make_hackathon(h, kind=EventKind.CONFERENCE)
    await make_hackathon(h, status=EventStatus.DRAFT)
    await set_phase(h, locked, FormationPhase.LOCKED)
    await set_phase(h, review, FormationPhase.REVIEW)
    await set_phase(h, closed, FormationPhase.CLOSED)
    await compute_lead_fits(h.runtime)
    jobs = await h.pending_jobs()
    assert [job["task_name"] for job in jobs] == ["compute_lead_fits", "compute_lead_fits"]
    assert {job["args"]["event_id"] for job in jobs} == {str(opened.id), str(locked.id)}


async def test_compute_skips_ineligible_events(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    await h.user()
    review = await make_hackathon(h)
    await set_phase(h, review, FormationPhase.REVIEW)
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    await compute_lead_fits(h.runtime, event_id=str(review.id))
    await compute_lead_fits(h.runtime, event_id=str(conference.id))
    assert await fits_of(h, review) == {}
    assert await fits_of(h, conference) == {}


async def test_compute_for_member(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    opened = await make_hackathon(h)
    locked = await make_hackathon(h)
    teamed = await make_hackathon(h)
    await set_phase(h, locked, FormationPhase.LOCKED)
    member = await h.user()
    other = await h.user()
    async with h.db() as session:
        await join_team(session, teamed, member)
    await compute_lead_fits_for_member(h.runtime, user_id=str(member.id))
    assert set(await fits_of(h, opened)) == {member.id}
    assert set(await fits_of(h, locked)) == {member.id}
    assert await fits_of(h, teamed) == {}
    assert other.id not in await fits_of(h, opened)
    banned = await h.user(banned_at=h.now)
    newbie = await h.user(onboarded=False)
    away = await h.user(dnd_indefinite=True)
    for user in (banned, newbie, away):
        await compute_lead_fits_for_member(h.runtime, user_id=str(user.id))
    await compute_lead_fits_for_member(h.runtime, user_id=str(uuid4()))
    for event in (opened, locked, teamed):
        assert not set(await fits_of(h, event)) & {banned.id, newbie.id, away.id}


def test_validated_lead_fits() -> None:
    a, b = sorted([uuid4(), uuid4()], key=str)
    result = service.validated_lead_fits(
        {a, b},
        [
            AlgoLeadFit.model_construct(user_id=a, fit=1.4, reason="r"),
            AlgoLeadFit.model_construct(user_id=b, fit=-0.2, reason="r"),
            AlgoLeadFit(user_id=uuid4(), fit=0.5, reason="r"),
            AlgoLeadFit(user_id=a, fit=0.3, reason="dup"),
        ],
    )
    assert [(fit.user_id, fit.fit, fit.reason) for fit in result] == [
        (a, 1.0, "r"),
        (b, 0.0, "r"),
    ]


async def test_get_my_lead_fit(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    draft = await make_hackathon(h, status=EventStatus.DRAFT)
    member = await h.user()
    newbie = await h.user(onboarded=False)
    url = f"/api/v1/events/{event.id}/lead-fit/me"
    async with await h.client(member) as c:
        assert_problem(await c.get(url), 503, ErrorCode.LEAD_FIT_PENDING)
        await add_fit(h, event, member, "0.9", "strong")
        response = await c.get(url)
        assert response.status_code == 200, response.text
        body = response.json()
        assert datetime.fromisoformat(body.pop("computed_at")) == h.now
        assert body == {"fit": 0.9, "eligible": True, "reason": "strong"}
        async with h.db() as session:
            await session.execute(
                update(models.LeadFit)
                .where(models.LeadFit.event_id == event.id)
                .values(fit=Decimal("0.4"))
            )
        response = await c.get(url)
        assert response.status_code == 200, response.text
        assert response.json()["eligible"] is False
        assert response.json()["fit"] == 0.4
        for event_id in (conference.id, uuid4(), draft.id):
            assert_problem(
                await c.get(f"/api/v1/events/{event_id}/lead-fit/me"), 404, ErrorCode.NOT_FOUND
            )
    async with h.anonymous() as c:
        assert_problem(await c.get(url), 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(newbie) as c:
        assert_problem(await c.get(url), 403, ErrorCode.ONBOARDING_REQUIRED)


async def test_list_lead_candidates(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    empty = await make_hackathon(h)
    conference = await make_hackathon(h, kind=EventKind.CONFERENCE)
    admin = await h.admin()
    lead = await h.user()
    strong = await h.user()
    keen = await h.user()
    plain = await h.user()
    async with h.db() as session:
        await join_team(session, event, lead)
        enrollments = {}
        for user in (strong, keen):
            enrollment = models.Enrollment(
                event_id=event.id,
                user_id=user.id,
                status=EnrollmentStatus.ACTIVE.value,
                source=EnrollmentSource.SELF.value,
                status_at=h.now,
                willing_to_lead=True,
            )
            session.add(enrollment)
            await session.flush()
            await session.refresh(enrollment)
            enrollments[user.id] = enrollment
    await add_fit(h, event, lead, "0.2")
    await add_fit(h, event, strong, "0.8")
    await add_fit(h, event, keen, "0.4")
    await add_fit(h, event, plain, "0.7")
    async with await h.client(admin) as c:
        response = await c.get(f"/api/v1/admin/events/{event.id}/lead-candidates")
        assert response.status_code == 200, response.text
        items = response.json()
        assert [
            (item["user"]["id"], item["tier"], item["fit"], item["willing_to_lead"])
            for item in items
        ] == [
            (str(lead.id), 1, 0.2, False),
            (str(strong.id), 2, 0.8, True),
            (str(keen.id), 3, 0.4, True),
            (str(plain.id), 4, 0.7, False),
        ]
        assert [item["eligible"] for item in items] == [False, True, False, True]
        assert items[0]["enrolled_at"] is None
        assert items[3]["enrolled_at"] is None
        assert datetime.fromisoformat(items[1]["enrolled_at"]) == enrollments[strong.id].created_at
        assert datetime.fromisoformat(items[2]["enrolled_at"]) == enrollments[keen.id].created_at
        response = await c.get(f"/api/v1/admin/events/{empty.id}/lead-candidates")
        assert response.status_code == 200, response.text
        assert response.json() == []
        for event_id in (conference.id, uuid4()):
            assert_problem(
                await c.get(f"/api/v1/admin/events/{event_id}/lead-candidates"),
                404,
                ErrorCode.NOT_FOUND,
            )
    async with await h.client(plain) as c:
        assert_problem(
            await c.get(f"/api/v1/admin/events/{event.id}/lead-candidates"),
            403,
            ErrorCode.FORBIDDEN,
        )


async def test_reads_do_not_enqueue(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    admin = await h.admin()
    member = await h.user()
    await add_fit(h, event, member, "0.7")
    before = await h.pending_jobs()
    async with await h.client(member) as c:
        assert (await c.get(f"/api/v1/events/{event.id}/lead-fit/me")).status_code == 200
    async with await h.client(admin) as c:
        response = await c.get(f"/api/v1/admin/events/{event.id}/lead-candidates")
        assert response.status_code == 200
    assert await h.pending_jobs() == before
