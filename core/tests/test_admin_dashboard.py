from datetime import datetime, timedelta

import pytest
from harness import Harness
from hmm_contracts.common import ErrorCode, FormationPhase
from hmm_contracts.notifications import AlertKind
from hmm_core import models
from hmm_core.admin.schemas import AdminDashboard
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from test_api_skeleton import assert_problem

PATH = "/api/v1/admin/dashboard"


async def add_event(
    session: AsyncSession,
    now: datetime,
    slug: str,
    *,
    origin: str = "manual",
    review_reasons: list[str] | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> models.Event:
    event = models.Event(
        slug=slug,
        kind="hackathon",
        formation_enabled=True,
        title=slug,
        starts_at=now + timedelta(days=5),
        ends_at=now + timedelta(days=6),
        timezone="UTC",
        language=["en"],
        themes=[],
        status="published",
        review_reasons=review_reasons or [],
        origin=origin,
        unmapped_areas=[],
        team_size_min=minimum,
        team_size_max=maximum,
    )
    session.add(event)
    await session.flush()
    return event


async def add_team(
    h: Harness,
    event: models.Event,
    status: str,
    size: int,
    *,
    lead: bool = True,
    seats_closed: bool = False,
    departed: bool = False,
) -> models.Project:
    members = [await h.user() for _ in range(size + int(departed))]
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name="Team",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            lead_user_id=members[0].id if lead and members else None,
            status="active",
            team_status=status,
            origin="formation",
            seats_closed_at=h.now if seats_closed else None,
        )
        session.add(project)
        await session.flush()
        session.add_all(
            models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=user.id,
                role="lead" if lead and index == 0 else "member",
                state="confirmed",
                trust="self",
                joined_via="formation",
                is_team_row=True,
                joined_at=h.now - timedelta(days=1),
                left_at=h.now if index == size else None,
            )
            for index, user in enumerate(members)
        )
        return project


async def get_dashboard(h: Harness) -> AdminDashboard:
    actor = await h.admin()
    async with await h.client(actor) as client:
        response = await client.get(PATH)
    assert response.status_code == 200, response.text
    return AdminDashboard.model_validate(response.json())


async def test_dashboard_empty(harness: Harness) -> None:
    dashboard = await get_dashboard(harness)
    assert dashboard.open_alerts == dict.fromkeys(AlertKind, 0)
    assert dashboard.hackathons_by_phase == dict.fromkeys(FormationPhase, 0)
    assert dashboard.parser_last_heartbeat is None
    counters = dashboard.model_dump(
        exclude={"open_alerts", "hackathons_by_phase", "parser_last_heartbeat"}
    )
    assert counters
    assert all(type(value) is int and value == 0 for value in counters.values())


async def test_dashboard_alerts_and_moderation(harness: Harness) -> None:
    h = harness
    user = await h.user()
    reviewed = await h.user()
    actor = await h.admin()
    async with h.db() as session:
        session.add_all(
            models.AdminAlert(type=kind, severity="warning", status=status, payload={})
            for kind, status in [
                ("team_below_min", "open"),
                ("team_below_min", "open"),
                ("algo_failure", "open"),
                ("job_failing", "resolved"),
            ]
        )
        session.add_all(
            [
                models.MemberReview(
                    user_id=user.id,
                    status="pending",
                    opened_at=h.now,
                    discrepancies=[],
                ),
                models.MemberReview(
                    user_id=reviewed.id,
                    status="reviewed",
                    opened_at=h.now,
                    reviewed_by=actor.id,
                    reviewed_at=h.now,
                    discrepancies=[],
                ),
            ]
        )
        session.add_all(
            models.Flag(
                user_id=user.id,
                subject_type="profile",
                subject_id=user.id,
                related_user_ids=[],
                reason="Review profile",
                severity="low",
                status=status,
                algo_version="test",
                created_at=h.now - timedelta(days=2),
            )
            for status in ["to_check", "pardoned"]
        )
        session.add_all(
            models.MemberRestriction(
                user_id=user.id,
                kind="confirming",
                reason="Restriction",
                created_by=actor.id,
                created_at=h.now - timedelta(days=2),
                until=until,
                lifted_at=lifted_at,
            )
            for until, lifted_at in [
                (None, None),
                (None, h.now - timedelta(hours=1)),
                (h.now - timedelta(hours=1), None),
                (h.now, None),
            ]
        )
    dashboard = await get_dashboard(h)
    assert dashboard.open_alerts == {
        kind: {"team_below_min": 2, "algo_failure": 1}.get(kind.value, 0) for kind in AlertKind
    }
    assert dashboard.pending_member_reviews == 1
    assert dashboard.to_check_items == 1
    assert dashboard.active_restrictions == 1
    async with h.db() as session:
        session.add(
            models.MemberRestriction(
                user_id=user.id,
                kind="mentoring",
                reason="Temporary restriction",
                created_by=actor.id,
                created_at=h.now,
                until=h.now + timedelta(hours=1),
            )
        )
    assert (await get_dashboard(h)).active_restrictions == 2


async def test_dashboard_catalog(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        await add_event(session, h.now, "clean")
        await add_event(
            session, h.now, "low-confidence", origin="import", review_reasons=["low_confidence"]
        )
        duplicate = await add_event(
            session,
            h.now,
            "member-duplicate",
            origin="member",
            review_reasons=["possible_duplicate", "low_confidence"],
        )
        duplicate.status = "archived"
    dashboard = await get_dashboard(h)
    assert dashboard.import_review_queue == 2
    assert dashboard.member_event_duplicates == 1
    async with h.db() as session:
        await add_event(
            session,
            h.now,
            "import-duplicate",
            origin="import",
            review_reasons=["possible_duplicate"],
        )
    dashboard = await get_dashboard(h)
    assert dashboard.import_review_queue == 3
    assert dashboard.member_event_duplicates == 1


async def test_dashboard_formation_and_teams(harness: Harness) -> None:
    h = harness
    async with h.db() as session:
        events: list[models.Event] = []
        phases = ["open", "review", "locked", "locked"]
        for index, phase in enumerate(phases):
            event = await add_event(session, h.now, f"event-{index}", minimum=3, maximum=5)
            events.append(event)
            session.add(
                models.EventFormation(
                    event_id=event.id,
                    phase=phase,
                    formation_at=h.now,
                    target_override=4,
                )
            )
    await add_team(h, events[0], "review", 2, lead=False, departed=True)
    await add_team(h, events[1], "locked", 3)
    await add_team(h, events[2], "locked", 3, seats_closed=True)
    await add_team(h, events[3], "finished", 1, lead=False)
    dashboard = await get_dashboard(h)
    assert dashboard.hackathons_by_phase == {
        phase: phases.count(phase.value) for phase in FormationPhase
    }
    assert dashboard.teams_below_min == 1
    assert dashboard.teams_without_lead == 1
    assert dashboard.open_seats == 1


@pytest.mark.parametrize(
    "status,size,minimum,maximum,target,below_min,open_seats,without_lead",
    [
        ("forming", 0, 3, 5, None, 0, 0, 1),
        ("review", 0, 3, 5, None, 1, 0, 1),
        ("active", 2, 3, 5, None, 1, 0, 1),
        ("locked", 2, 3, 5, None, 1, 3, 1),
        ("locked", 6, 3, 5, 4, 0, 0, 1),
        ("locked", 3, None, None, None, 1, 3, 1),
        ("locked", 4, None, 5, None, 0, 1, 1),
        ("locked", 4, None, 5, 7, 0, 3, 1),
        ("finished", 1, 3, 5, None, 0, 0, 0),
    ],
)
async def test_dashboard_team_sizing(
    harness: Harness,
    status: str,
    size: int,
    minimum: int | None,
    maximum: int | None,
    target: int | None,
    below_min: int,
    open_seats: int,
    without_lead: int,
) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        session.add_all(
            [
                models.Setting(key="team_size_default_min", value=4, updated_by=actor.id),
                models.Setting(key="team_size_default_max", value=6, updated_by=actor.id),
            ]
        )
        event = await add_event(session, h.now, "team-sizing", minimum=minimum, maximum=maximum)
        if target is not None:
            session.add(
                models.EventFormation(
                    event_id=event.id,
                    phase="locked",
                    formation_at=h.now,
                    target_override=target,
                )
            )
    await add_team(h, event, status, size, lead=False)
    dashboard = await get_dashboard(h)
    assert dashboard.teams_below_min == below_min
    assert dashboard.open_seats == open_seats
    assert dashboard.teams_without_lead == without_lead


async def test_dashboard_results_and_help(harness: Harness) -> None:
    h = harness
    learners = [await h.user() for _ in range(3)]
    mentor = await h.user()
    areas = await h.seed_areas(("backend",))
    area = areas["backend"]
    async with h.db() as session:
        event = await add_event(session, h.now, "results")
    projects = [await add_team(h, event, "finished", 1) for _ in range(2)]
    async with h.db() as session:
        session.add_all(
            models.ProjectResult(
                project_id=project.id,
                outcome="finished",
                place=place,
                place_status=status,
                member_areas={},
                participants=[],
            )
            for project, place, status in zip(
                projects, [1, 2], ["unverified", "verified"], strict=True
            )
        )
        requests = [
            models.HelpRequest(
                learner_id=learner.id,
                area_id=area.id,
                topic="Backend",
                goal="Improve",
                format="online",
                status=status,
                expires_at=h.now + timedelta(days=1),
            )
            for learner, status in zip(learners, ["open", "open", "matched"], strict=True)
        ]
        session.add_all(requests)
        await session.flush()
        session.add(
            models.HelpOffer(
                request_id=requests[1].id,
                mentor_id=mentor.id,
                message="Available",
                status="pending",
            )
        )
    dashboard = await get_dashboard(h)
    assert dashboard.results_to_verify == 1
    assert dashboard.open_help_requests_without_offers == 1
    async with h.db() as session:
        session.add(
            models.HelpOffer(
                request_id=requests[0].id,
                mentor_id=mentor.id,
                message="Withdrawn",
                status="withdrawn",
            )
        )
    assert (await get_dashboard(h)).open_help_requests_without_offers == 1


async def test_dashboard_parser_and_jobs(harness: Harness) -> None:
    h = harness
    latest = h.now - timedelta(hours=1)
    async with h.db() as session:
        session.add_all(
            models.ParserHeartbeat(
                run_id=f"run-{index}",
                started_at=h.now - timedelta(hours=4),
                finished_at=finished_at,
                stats={},
                errors=[],
            )
            for index, finished_at in enumerate([latest, h.now - timedelta(hours=2), None])
        )
        for status, at, event_count in [
            ("failed", latest, 2),
            ("failed", h.now - timedelta(hours=48), 1),
            ("succeeded", latest, 1),
        ]:
            job_id = await session.scalar(
                text(
                    "INSERT INTO procrastinate.procrastinate_jobs "
                    "(queue_name, task_name, status, attempts) "
                    "VALUES ('default', 'cleanup', CAST(:status AS procrastinate_job_status), 5) "
                    "RETURNING id"
                ),
                {"status": status},
            )
            for _ in range(event_count):
                await session.execute(
                    text(
                        "INSERT INTO procrastinate.procrastinate_events (job_id, type, at) "
                        "VALUES (:id, 'failed'::procrastinate_job_event_type, :at)"
                    ),
                    {"id": job_id, "at": at},
                )
    dashboard = await get_dashboard(h)
    assert dashboard.parser_last_heartbeat == latest
    assert dashboard.job_failures_24h == 1


async def test_dashboard_access(harness: Harness) -> None:
    h = harness
    member = await h.user()
    actor = await h.admin()
    telegram_id = await h.link_telegram(actor)
    async with await h.client(member) as client:
        assert_problem(await client.get(PATH), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as client:
        assert_problem(await client.get(PATH), 401, ErrorCode.UNAUTHENTICATED)
    async with h.bot(telegram_id) as client:
        assert_problem(await client.get(PATH), 401, ErrorCode.UNAUTHENTICATED)
