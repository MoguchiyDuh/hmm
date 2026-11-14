import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from harness import Harness
from hmm_contracts.algo import (
    AlgoSettings,
    FormationPlan,
    FormationRequest,
    PlannedMember,
    PlannedMerge,
    PlannedTeam,
    RankedMember,
)
from hmm_contracts.common import (
    AreaImportance,
    EventKind,
    EventOrigin,
    EventStatus,
    FormationKind,
    FormationPhase,
)
from hmm_contracts.formation import Bounds
from hmm_core import models
from hmm_core.catalog import service as catalog
from hmm_core.formation import service
from hmm_core.platform.settings import load_settings
from pydantic import JsonValue
from sqlalchemy import select, update

_titles = itertools.count(1)


async def make_hackathon(
    h: Harness,
    *,
    team_size_min: int | None = None,
    team_size_max: int | None = None,
    starts_in: timedelta = timedelta(days=30),
    registration_deadline: datetime | None = None,
    areas: Sequence[tuple[str, AreaImportance]] = (("backend", AreaImportance.PRIMARY),),
) -> models.Event:
    async with h.db() as session:
        event = await catalog.create_event(
            session,
            catalog.EventInput(
                kind=EventKind.HACKATHON,
                title=f"Run Hack {next(_titles)}",
                starts_at=h.now + starts_in,
                ends_at=h.now + starts_in + timedelta(days=2),
                timezone="UTC",
                registration_deadline=registration_deadline,
                team_size_min=team_size_min,
                team_size_max=team_size_max,
                formation_enabled=True,
                areas=[
                    catalog.EventAreaInput(key=key, importance=importance)
                    for key, importance in areas
                ],
            ),
            origin=EventOrigin.MANUAL,
            status=EventStatus.PUBLISHED,
            created_by=None,
            now=h.now,
        )
        await service.ensure_formation(session, event, h.now)
        return event


async def set_phase(
    h: Harness, event: models.Event, phase: FormationPhase, *, rounds: int = 0
) -> models.EventFormation:
    async with h.db() as session:
        await session.execute(
            update(models.EventFormation)
            .where(models.EventFormation.event_id == event.id)
            .values(phase=phase.value, rounds=rounds)
        )
        formation = await session.get_one(models.EventFormation, event.id)
        await session.refresh(formation)
        return formation


async def enroll(
    h: Harness,
    event: models.Event,
    user: models.User,
    *,
    status: str = "active",
    willing: bool = False,
    area: models.Area | None = None,
    created_at: datetime | None = None,
) -> models.Enrollment:
    async with h.db() as session:
        enrollment = models.Enrollment(
            event_id=event.id,
            user_id=user.id,
            status=status,
            source="self",
            willing_to_lead=willing,
            preferred_area_id=area.id if area is not None else None,
            status_at=h.now,
            note="",
        )
        session.add(enrollment)
        await session.flush()
        await session.execute(
            update(models.Enrollment)
            .where(models.Enrollment.id == enrollment.id)
            .values(created_at=created_at or h.now)
        )
        await session.refresh(enrollment)
        return enrollment


async def make_core(
    h: Harness,
    event: models.Event,
    name: str,
    *,
    lead: models.User | None = None,
    members: Sequence[models.User] = (),
    team_status: str = "forming",
    origin: str = "claim",
) -> models.Project:
    async with h.db() as session:
        project = models.Project(
            event_id=event.id,
            name=name,
            description_md="",
            goal="",
            links=[],
            visibility="public",
            recruiting=None,
            lead_user_id=lead.id if lead is not None else None,
            status="idea",
            team_status=team_status,
            origin=origin,
        )
        session.add(project)
        await session.flush()
        people: list[tuple[models.User, str]] = [(lead, "lead")] if lead is not None else []
        people.extend((member, "member") for member in members)
        for user, role in people:
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=user.id,
                    role=role,
                    state="confirmed",
                    trust="self",
                    joined_via="claim",
                    is_team_row=True,
                    joined_at=h.now,
                    confirmed_at=h.now,
                )
            )
            if await service.live_enrollment_row(session, event.id, user.id) is None:
                session.add(
                    models.Enrollment(
                        event_id=event.id,
                        user_id=user.id,
                        status="placed",
                        source="self",
                        willing_to_lead=False,
                        status_at=h.now,
                        note="",
                        created_at=h.now,
                    )
                )
            await session.flush()
        await session.refresh(project)
        return project


async def add_fit(h: Harness, event: models.Event, user: models.User, fit: float) -> None:
    async with h.db() as session:
        session.add(
            models.LeadFit(
                event_id=event.id,
                user_id=user.id,
                fit=Decimal(str(fit)),
                reason="fit",
                algo_version="fake-1",
                computed_at=h.now,
            )
        )


async def request_for(
    h: Harness,
    event: models.Event,
    kind: FormationKind = FormationKind.MAIN,
    phase: FormationPhase = FormationPhase.OPEN,
) -> FormationRequest:
    async with h.db() as session:
        current = await session.get_one(models.Event, event.id)
        formation = await session.get_one(models.EventFormation, event.id)
        return await service.build_formation_request(
            session, current, formation, kind, phase, h.now
        )


async def fake_plan(h: Harness, request: FormationRequest) -> tuple[FormationPlan, AlgoSettings]:
    async with h.db() as session:
        ctx = await h.runtime.algo_context(session)
    return await h.algorithm.form_teams(ctx, request), ctx.settings


def constraints(errors: list[JsonValue]) -> list[int]:
    return [cast(int, cast(dict[str, JsonValue], error)["constraint"]) for error in errors]


def request_user_ids(request: FormationRequest) -> list[UUID]:
    return (
        [core.lead.user_id for core in request.cores]
        + [member.user_id for core in request.cores for member in core.members]
        + [member.user_id for member in request.unplaced]
    )


async def ensure_areas(h: Harness) -> None:
    async with h.db() as session:
        seeded = await session.scalar(select(models.Area.id).limit(1))
    if seeded is None:
        await h.seed_areas()


async def scenario_two_cores(h: Harness, free: int) -> tuple[models.Event, list[models.Project]]:
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    cores = []
    for index in range(2):
        lead, member = await h.user(), await h.user()
        cores.append(await make_core(h, event, f"Core {index}", lead=lead, members=[member]))
    for _ in range(free):
        user = await h.user()
        await enroll(h, event, user)
        await add_fit(h, event, user, 0.5)
    return event, cores


async def scenario_mixed(h: Harness) -> FormationRequest:
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    lead, member = await h.user(), await h.user()
    await make_core(h, event, "Core", lead=lead, members=[member])
    for index in range(12):
        user = await h.user()
        await enroll(h, event, user, willing=index % 2 == 0)
        await add_fit(h, event, user, round(0.95 - index * 0.05, 2))
    return await request_for(h, event)


def new_team_index(plan: FormationPlan) -> int:
    return next(index for index, team in enumerate(plan.teams) if team.project_id is None)


def core_team_index(plan: FormationPlan) -> int:
    return next(index for index, team in enumerate(plan.teams) if team.project_id is not None)


def replace_team(plan: FormationPlan, index: int, team: PlannedTeam) -> FormationPlan:
    teams = list(plan.teams)
    teams[index] = team
    return plan.model_copy(update={"teams": teams})


async def test_build_request_main_open_shape(harness: Harness) -> None:
    h = harness
    event, _ = await scenario_two_cores(h, 10)
    request = await request_for(h, event)
    assert request.bounds == Bounds(teams=2, new_teams=0, placed=8, reserve=2, undersized_cores=0)
    assert (request.target, request.viable, request.max_size) == (6, 5, 6)
    assert len(request.cores) == 2
    assert all(len(core.members) == 1 for core in request.cores)
    assert all(core.lead.claimed_lead for core in request.cores)
    assert len(request.unplaced) == 10
    assert len(request.lead_fits) == 10
    assert request.kind == "main"
    assert request.hackathon.id == event.id


async def test_build_request_placeholder_lead(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    await set_phase(h, event, FormationPhase.REVIEW, rounds=1)
    willing, other = await h.user(), await h.user()
    await enroll(h, event, willing, status="placed", willing=True)
    await enroll(h, event, other, status="placed")
    await add_fit(h, event, willing, 0.8)
    await add_fit(h, event, other, 0.4)
    project = await make_core(h, event, "Review", members=[other, willing], team_status="review")
    request = await request_for(h, event, phase=FormationPhase.REVIEW)
    [core] = request.cores
    assert core.project_id == project.id
    assert core.lead.user_id == willing.id
    assert core.lead.claimed_lead is False
    assert [member.user_id for member in core.members] == [other.id]
    async with h.db() as session:
        stored = await session.get_one(models.Project, project.id)
        assert stored.lead_user_id is None


async def test_build_request_free_pool_by_phase(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    member = await h.user()
    team = await make_core(h, event, "Review", members=[member], team_status="review")
    reserve = [await h.user(), await h.user()]
    for user in reserve:
        await enroll(h, event, user, status="reserve")
    released = await h.user()
    await enroll(h, event, released)

    review = await request_for(h, event, phase=FormationPhase.REVIEW)
    assert [core.project_id for core in review.cores] == [team.id]
    assert len(review.unplaced) == 3

    opened = await request_for(h, event, phase=FormationPhase.OPEN)
    assert opened.cores == []
    assert [entry.user_id for entry in opened.unplaced] == [released.id]

    late_lead = await h.user()
    late = await make_core(h, event, "Late", lead=late_lead, origin="late_claim")
    late_request = await request_for(h, event, FormationKind.LATE, FormationPhase.LOCKED)
    assert [core.project_id for core in late_request.cores] == [late.id]
    assert sorted(entry.user_id for entry in late_request.unplaced) == sorted(
        user.id for user in reserve
    )


async def test_build_request_excludes_banned_placed_and_unonboarded(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    banned = await h.user(banned_at=h.now)
    await enroll(h, event, banned)
    lead = await h.user()
    await enroll(h, event, lead)
    await make_core(h, event, "Core", lead=lead)
    pending = await h.user(onboarded=False)
    await enroll(h, event, pending)
    free = await h.user()
    await enroll(h, event, free)
    request = await request_for(h, event)
    assert [entry.user_id for entry in request.unplaced] == [free.id]
    assert [core.lead.user_id for core in request.cores] == [lead.id]
    ids = request_user_ids(request)
    assert len(ids) == len(set(ids))


async def test_validate_fake_plans(harness: Harness) -> None:
    h = harness
    event, _ = await scenario_two_cores(h, 10)
    request = await request_for(h, event)
    plan, settings = await fake_plan(h, request)
    assert service.validate_plan(request, plan, settings) == []

    merge_event, _ = await scenario_two_cores(h, 1)
    merge_request = await request_for(h, merge_event)
    assert merge_request.bounds.placed == 1
    assert merge_request.bounds.undersized_cores == 2
    merge_plan, settings = await fake_plan(h, merge_request)
    assert merge_plan.merges
    assert service.validate_plan(merge_request, merge_plan, settings) == []


async def test_validate_constraint_1(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    plan, settings = await fake_plan(h, request)
    assert service.validate_plan(request, plan, settings) == []
    new_index = new_team_index(plan)
    new_team = plan.teams[new_index]

    removed = replace_team(
        plan, new_index, new_team.model_copy(update={"members": new_team.members[:-1]})
    )
    assert constraints(service.validate_plan(request, removed, settings)).count(1) == 1

    core_index = core_team_index(plan)
    core_team = plan.teams[core_index]
    duplicated = replace_team(
        plan,
        core_index,
        core_team.model_copy(update={"members": [*core_team.members, new_team.members[-1]]}),
    )
    assert 1 in constraints(service.validate_plan(request, duplicated, settings))

    stranger = PlannedMember(user_id=uuid4(), area=None, reason="Unknown")
    unknown = replace_team(
        plan, core_index, core_team.model_copy(update={"members": [*core_team.members, stranger]})
    )
    assert 1 in constraints(service.validate_plan(request, unknown, settings))


async def test_validate_constraint_2(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    plan, settings = await fake_plan(h, request)
    [core] = request.cores
    core_index, new_index = core_team_index(plan), new_team_index(plan)
    core_team, new_team = plan.teams[core_index], plan.teams[new_index]

    moving = next(
        member for member in core_team.members if member.user_id == core.members[0].user_id
    )
    moved = replace_team(
        replace_team(
            plan,
            core_index,
            core_team.model_copy(
                update={"members": [m for m in core_team.members if m is not moving]}
            ),
        ),
        new_index,
        new_team.model_copy(update={"members": [*new_team.members, moving]}),
    )
    assert 2 in constraints(service.validate_plan(request, moved, settings))

    led = replace_team(
        plan, core_index, core_team.model_copy(update={"lead_user_id": core.lead.user_id})
    )
    assert 2 in constraints(service.validate_plan(request, led, settings))

    foreign = plan.model_copy(
        update={
            "teams": [
                *plan.teams,
                PlannedTeam(project_id=uuid4(), lead_user_id=None, members=[], reason="Ghost"),
            ]
        }
    )
    assert 2 in constraints(service.validate_plan(request, foreign, settings))


async def test_validate_constraint_3(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    plan, settings = await fake_plan(h, request)
    new_index = new_team_index(plan)
    new_team = plan.teams[new_index]

    extra = plan.reserve[0]
    padded = replace_team(
        plan,
        new_index,
        new_team.model_copy(
            update={
                "members": [
                    *new_team.members,
                    PlannedMember(user_id=extra.user_id, area=None, reason="Pad"),
                ]
            }
        ),
    ).model_copy(
        update={
            "reserve": [
                ranked.model_copy(update={"rank": rank})
                for rank, ranked in enumerate(plan.reserve[1:], start=1)
            ]
        }
    )
    assert 3 in constraints(service.validate_plan(request, padded, settings))

    dropped = new_team.members[-2:]
    shrunk = replace_team(
        plan, new_index, new_team.model_copy(update={"members": new_team.members[:-2]})
    ).model_copy(
        update={
            "reserve": [
                *plan.reserve,
                *(
                    RankedMember(
                        user_id=member.user_id, rank=len(plan.reserve) + offset, reason="x"
                    )
                    for offset, member in enumerate(dropped, start=1)
                ),
            ]
        }
    )
    assert 3 in constraints(service.validate_plan(request, shrunk, settings))

    merge_event, _ = await scenario_two_cores(h, 1)
    merge_request = await request_for(h, merge_event)
    merge_plan, settings = await fake_plan(h, merge_request)
    unmerged = merge_plan.model_copy(update={"merges": []})
    assert 3 in constraints(service.validate_plan(merge_request, unmerged, settings))


async def test_validate_constraint_4(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    plan, settings = await fake_plan(h, request)
    new_index = new_team_index(plan)
    new_team = plan.teams[new_index]

    benched = new_team.members[-1]
    moved = replace_team(
        plan, new_index, new_team.model_copy(update={"members": new_team.members[:-1]})
    ).model_copy(
        update={
            "reserve": [
                *plan.reserve,
                RankedMember(user_id=benched.user_id, rank=len(plan.reserve) + 1, reason="x"),
            ]
        }
    )
    assert constraints(service.validate_plan(request, moved, settings)) == [4, 4]

    duplicate_rank = plan.model_copy(
        update={"reserve": [ranked.model_copy(update={"rank": 1}) for ranked in plan.reserve]}
    )
    assert 4 in constraints(service.validate_plan(request, duplicate_rank, settings))

    empty = plan.model_copy(
        update={
            "teams": [
                *plan.teams,
                PlannedTeam(project_id=None, lead_user_id=None, members=[], reason="Empty"),
            ]
        }
    )
    assert constraints(service.validate_plan(request, empty, settings)).count(4) == 2


async def test_validate_constraint_5(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    plan, settings = await fake_plan(h, request)
    new_index = new_team_index(plan)
    new_team = plan.teams[new_index]
    assert new_team.lead_user_id == new_team.members[0].user_id

    worse = replace_team(
        plan,
        new_index,
        new_team.model_copy(update={"lead_user_id": new_team.members[1].user_id}),
    )
    assert constraints(service.validate_plan(request, worse, settings)) == [5]

    missing = replace_team(plan, new_index, new_team.model_copy(update={"lead_user_id": None}))
    assert constraints(service.validate_plan(request, missing, settings)) == [5]

    outsider = plan.teams[core_team_index(plan)].members[0].user_id
    foreign = replace_team(plan, new_index, new_team.model_copy(update={"lead_user_id": outsider}))
    assert constraints(service.validate_plan(request, foreign, settings)) == [5]


async def test_validate_constraint_6(harness: Harness) -> None:
    h = harness
    event, cores = await scenario_two_cores(h, 10)
    request = await request_for(h, event)
    plan, settings = await fake_plan(h, request)
    first, second = cores[0].id, cores[1].id
    merged = plan.model_copy(
        update={
            "merges": [
                PlannedMerge(absorbed_team_id=uuid4(), target_team_id=first, reason="Unknown"),
                PlannedMerge(absorbed_team_id=first, target_team_id=first, reason="Self"),
                PlannedMerge(absorbed_team_id=first, target_team_id=second, reason="Too big"),
            ]
        }
    )
    assert constraints(service.validate_plan(request, merged, settings)) == [6, 6, 6]


async def test_planned_lead_user(harness: Harness) -> None:
    h = harness
    await h.seed_areas()
    event = await make_hackathon(h)
    strong, weak, outsider = await h.user(), await h.user(), await h.user()
    await enroll(h, event, strong, willing=True)
    await enroll(h, event, weak, willing=True)
    await enroll(h, event, outsider)
    await add_fit(h, event, strong, 0.8)
    await add_fit(h, event, weak, 0.4)
    await add_fit(h, event, outsider, 0.9)
    async with h.db() as session:
        settings = await load_settings(session)
    request = await request_for(h, event)
    facts = service.formation_member_facts(request, settings)
    assert service.planned_lead_user(facts, [outsider.id, weak.id, strong.id]) == strong.id

    plain = await make_hackathon(h)
    willing, other = await h.user(), await h.user()
    await enroll(h, plain, other)
    await enroll(h, plain, willing, willing=True)
    plain_request = await request_for(h, plain)
    plain_facts = service.formation_member_facts(plain_request, settings)
    assert plain_request.lead_fits == {}
    assert service.planned_lead_user(plain_facts, [other.id, willing.id]) == willing.id
    assert service.planned_lead_user(plain_facts, []) is None


async def fallback_for(h: Harness, request: FormationRequest) -> tuple[FormationPlan, AlgoSettings]:
    async with h.db() as session:
        settings = (await h.runtime.algo_context(session)).settings
    return service.fallback_plan(request, settings), settings


async def area_ids(h: Harness) -> dict[str, UUID]:
    async with h.db() as session:
        return {area.key: area.id for area in await session.scalars(select(models.Area))}


async def seed_score(h: Harness, user: models.User, area_id: UUID, value: int) -> None:
    async with h.db() as session:
        session.add(
            models.AreaScore(
                user_id=user.id, area_id=area_id, calculated=Decimal(value), calc_reason="x"
            )
        )


async def test_fallback_main_open_shape(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    for index in range(2):
        lead, member = await h.user(), await h.user()
        await make_core(h, event, f"Core {index}", lead=lead, members=[member])
    free = []
    for index in range(10):
        user = await h.user()
        await enroll(h, event, user, created_at=h.now + timedelta(minutes=index))
        free.append(user)
    request = await request_for(h, event)
    plan, settings = await fallback_for(h, request)
    assert len(plan.teams) == 2
    assert sorted(len(team.members) for team in plan.teams) == [6, 6]
    assert [ranked.rank for ranked in plan.reserve] == [1, 2]
    assert [ranked.user_id for ranked in plan.reserve] == [free[8].id, free[9].id]
    assert plan.merges == []
    assert all(team.project_id is not None and team.lead_user_id is None for team in plan.teams)
    assert service.validate_plan(request, plan, settings) == []


async def test_fallback_seeds_volunteers_by_tier(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=3)
    strong, weak = await h.user(), await h.user()
    plain = [await h.user() for _ in range(4)]
    for user in plain[:2]:
        await enroll(h, event, user)
    await enroll(h, event, weak, willing=True)
    await enroll(h, event, strong, willing=True)
    for user in plain[2:]:
        await enroll(h, event, user)
    await add_fit(h, event, strong, 0.9)
    await add_fit(h, event, weak, 0.3)
    request = await request_for(h, event)
    assert (request.bounds.new_teams, request.bounds.placed) == (2, 6)
    plan, settings = await fallback_for(h, request)
    assert [len(team.members) for team in plan.teams] == [3, 3]
    assert [team.lead_user_id for team in plan.teams] == [strong.id, weak.id]
    assert all(
        team.lead_user_id in {member.user_id for member in team.members} for team in plan.teams
    )
    assert plan.reserve == []
    assert service.validate_plan(request, plan, settings) == []


async def test_fallback_area_coverage(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    ids = await area_ids(h)
    areas = (("ml_ai", AreaImportance.PRIMARY), ("backend", AreaImportance.REQUIRED))
    event = await make_hackathon(h, team_size_min=3, team_size_max=3, areas=areas)
    scored = []
    for value in (4, 9, 6):
        user = await h.user()
        await enroll(h, event, user)
        await seed_score(h, user, ids["ml_ai"], value)
        scored.append(user)
    request = await request_for(h, event)
    plan, settings = await fallback_for(h, request)
    for team in plan.teams:
        if team.project_id is None:
            assert scored[1].id in {member.user_id for member in team.members}
    assert service.validate_plan(request, plan, settings) == []

    core_event = await make_hackathon(h, team_size_min=3, team_size_max=3, areas=areas)
    lead = await h.user()
    await make_core(h, core_event, "Core", lead=lead)
    for _ in range(3):
        await enroll(h, core_event, await h.user())
    expert = await h.user()
    await enroll(h, core_event, expert, created_at=h.now + timedelta(hours=1))
    await seed_score(h, expert, ids["ml_ai"], 9)
    core_request = await request_for(h, core_event)
    core_plan, settings = await fallback_for(h, core_request)
    [core_team] = core_plan.teams
    assert {"ml_ai", "backend"} <= {member.area for member in core_team.members}
    assert next(member for member in core_team.members if member.user_id == expert.id).area == (
        "ml_ai"
    )
    assert service.validate_plan(core_request, core_plan, settings) == []


async def test_fallback_undersized_cores_merge(harness: Harness) -> None:
    h = harness
    event, cores = await scenario_two_cores(h, 1)
    request = await request_for(h, event)
    assert (request.bounds.placed, request.bounds.undersized_cores) == (1, 2)
    assert request.bounds.new_teams == 0
    plan, settings = await fallback_for(h, request)
    sizes = {team.project_id: len(team.members) for team in plan.teams}
    assert all(size < request.viable for size in sizes.values())
    [merge] = plan.merges
    assert (sizes[merge.absorbed_team_id], sizes[merge.target_team_id]) == (2, 3)
    assert {merge.absorbed_team_id, merge.target_team_id} == {core.id for core in cores}
    assert service.validate_plan(request, plan, settings) == []

    full_event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    for index in range(2):
        lead = await h.user()
        members = [await h.user() for _ in range(3)]
        await make_core(h, full_event, f"Core {index}", lead=lead, members=members)
    full_request = await request_for(h, full_event)
    full_plan, settings = await fallback_for(h, full_request)
    assert full_plan.merges == []
    assert service.validate_plan(full_request, full_plan, settings) == []


async def test_fallback_is_deterministic(harness: Harness) -> None:
    h = harness
    request = await scenario_mixed(h)
    first, settings = await fallback_for(h, request)
    second = service.fallback_plan(request, settings)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert service.validate_plan(request, first, settings) == []


async def test_fallback_empty_request(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=4, team_size_max=5)
    request = await request_for(h, event)
    assert (request.target, request.viable, request.max_size) == (5, 4, 5)
    plan, settings = await fallback_for(h, request)
    assert (plan.teams, plan.reserve, plan.merges) == ([], [], [])
    assert service.validate_plan(request, plan, settings) == []


async def test_fallback_core_headroom(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    lead, member = await h.user(), await h.user()
    await make_core(h, event, "Core", lead=lead, members=[member])
    for _ in range(5):
        await enroll(h, event, await h.user())
    request = await request_for(h, event)
    assert request.bounds == Bounds(teams=1, new_teams=0, placed=4, reserve=1, undersized_cores=0)
    plan, settings = await fallback_for(h, request)
    [team] = plan.teams
    assert len(team.members) == 6
    assert len(plan.reserve) == 1
    assert plan.merges == []
    assert service.validate_plan(request, plan, settings) == []


async def test_fallback_full_core_opens_new_team(harness: Harness) -> None:
    h = harness
    await ensure_areas(h)
    event = await make_hackathon(h, team_size_min=3, team_size_max=6)
    lead = await h.user()
    members = [await h.user() for _ in range(5)]
    core = await make_core(h, event, "Core", lead=lead, members=members)
    for _ in range(5):
        await enroll(h, event, await h.user())
    request = await request_for(h, event)
    assert (request.bounds.new_teams, request.bounds.placed, request.bounds.reserve) == (1, 5, 0)
    plan, settings = await fallback_for(h, request)
    core_team = next(team for team in plan.teams if team.project_id == core.id)
    assert [member.user_id for member in core_team.members] == [
        lead.id,
        *(member.id for member in members),
    ]
    new_teams = [team for team in plan.teams if team.project_id is None]
    assert [len(team.members) for team in new_teams] == [5]
    assert service.validate_plan(request, plan, settings) == []
