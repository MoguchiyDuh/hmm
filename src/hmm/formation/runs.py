from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.algo.models import (
    EnrolledMember,
    EventArea,
    EventSnapshot,
    FormationPlan,
    FormationRequest,
    FormationUnit,
)
from hmm.algo.pack import Packing, Unit, pack
from hmm.common import ActorKind, AuditAction, AuditEntityType, ErrorCode
from hmm.formation import schemas
from hmm.formation.common import (
    accept_by,
    audit,
    formation_row,
    formation_sizes,
    formation_time,
    locked_formation_event,
    members,
    open_teams,
    participation_event,
    pick_lead,
    queue_units,
    reg_deadline,
)
from hmm.formation.invites import close_invite
from hmm.formation.queue import formation_participation
from hmm.formation.teams import assign_lead
from hmm.notifications.payloads import (
    ActionKey,
    AlertKind,
    ApiAction,
    ReservePlacedPayload,
    TeamProposedPayload,
)
from hmm.notifications.service import notify, notify_many, raise_alert, resolve_alerts
from hmm.platform.algo import call_algo
from hmm.platform.audit import record_audit
from hmm.platform.errors import ProblemError
from hmm.platform.jobs import JobName, Runtime, defer_job
from hmm.platform.schemas import ReasonBody, notification_person
from hmm.platform.settings import load_settings
from hmm.scores import snapshot as scores_snapshot


async def build_formation_request(
    session: AsyncSession,
    event: models.Event,
    units: list[Unit],
    grouped: dict[UUID, list[models.Enrollment]],
) -> FormationRequest:

    snapshots = await scores_snapshot.build_member_snapshots(
        session, [row.user_id for unit in units for row in grouped[unit.id]]
    )
    areas = {row.id: row.key for row in await session.scalars(select(models.Area))}
    event_areas = [
        EventArea(area=areas[row.area_id], importance=row.importance, headcount=row.headcount)
        for row in await session.scalars(
            select(models.EventArea).where(models.EventArea.event_id == event.id)
        )
    ]
    snapshot = EventSnapshot.model_validate(
        {
            **{
                key: getattr(event, key)
                for key in (
                    "id",
                    "title",
                    "summary",
                    "description_md",
                    "themes",
                    "format",
                    "starts_at",
                    "ends_at",
                    "team_size_min",
                    "team_size_max",
                    "difficulty",
                    "kind",
                    "origin",
                    "created_by",
                    "timezone",
                )
            },
            "areas": event_areas,
        }
    )
    target, minimum, maximum = formation_sizes(
        event, await formation_row(session, event.id), await load_settings(session)
    )
    return FormationRequest(
        hackathon=snapshot,
        target=target,
        min_size=minimum,
        max_size=maximum,
        units=[
            FormationUnit(
                id=unit.id,
                members=[
                    EnrolledMember(
                        **snapshots[row.user_id].model_dump(),
                        preferred_area=areas.get(row.preferred_area_id)
                        if row.preferred_area_id
                        else None,
                        willing_to_lead=row.willing_to_lead,
                        enrolled_at=unit.queued_at,
                        note=row.note or "",
                    )
                    for row in grouped[unit.id]
                ],
            )
            for unit in units
        ],
    )


def validate_plan(
    plan: FormationPlan, units: list[Unit], baseline: Packing, minimum: int, maximum: int
) -> list[str]:
    sizes = {unit.id: unit.size for unit in units}
    errors: list[str] = []
    seen: set[UUID] = set()
    for team in plan.teams:
        for unit_id in team.units:
            if unit_id not in sizes:
                errors.append(f"Unknown unit {unit_id}")
            if unit_id in seen:
                errors.append(f"Duplicate unit {unit_id}")
            seen.add(unit_id)
        if not minimum <= sum(sizes.get(unit_id, 0) for unit_id in team.units) <= maximum:
            errors.append("Team size is outside bounds")
    if sum(sizes.get(unit_id, 0) for unit_id in seen) < sum(
        sizes[unit_id] for team in baseline.new_teams for unit_id in team
    ):
        errors.append("Plan places fewer people than pack")
    return errors


async def apply_packing(
    session: AsyncSession,
    event: models.Event,
    packing: Packing,
    grouped: dict[UUID, list[models.Enrollment]],
    parties: list[models.Party],
    now: datetime,
    *,
    refill: bool = False,
) -> None:
    settings = await load_settings(session)
    deadline = accept_by(event, settings, now)
    targets: list[tuple[models.Project, list[UUID]]] = []
    for project_id, unit_ids in packing.fills.items():
        project = await session.get(models.Project, project_id)
        if project is not None:
            targets.append((project, unit_ids))
    for index, unit_ids in enumerate(packing.new_teams):
        project = models.Project(
            event_id=event.id,
            name=f"{event.title[:75]} — {now:%m%d%H%M}-{index + 1}",
            description_md="",
            goal="",
            links=[],
            visibility="public",
            status="idea",
            team_status="formed",
            origin="formation",
            lead_user_id=None,
            seats_closed_at=now
            if any(party.closed and party.id in unit_ids for party in parties)
            else None,
        )
        session.add(project)
        await session.flush()
        targets.append((project, unit_ids))
    placed_parties: set[UUID] = set()
    for project, unit_ids in targets:
        rows = [row for unit_id in unit_ids for row in grouped[unit_id]]
        team_parties = [party for party in parties if party.id in unit_ids]
        lead = (
            await pick_lead(session, rows, team_parties)
            if project.lead_user_id is None
            else project.lead_user_id
        )
        for row in rows:
            session.add(
                models.ProjectMember(
                    project_id=project.id,
                    event_id=event.id,
                    user_id=row.user_id,
                    role="member",
                    area_id=row.preferred_area_id,
                    state="proposed",
                    trust="self",
                    joined_via="refill" if refill else "formation",
                    accept_by=deadline,
                    is_team_row=True,
                    joined_at=now,
                )
            )
            row.status = "placed"
            row.party_id = None
        placed_parties.update(party.id for party in team_parties)
        await session.flush()
        await assign_lead(session, project, event, lead, now, reason="Team placement")
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.MEMBER_ADD,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            after={
                "users": [str(row.user_id) for row in rows],
                "parties": [
                    {
                        "owner": str(party.owner_user_id),
                        "id": str(party.id),
                        "size": len(grouped[party.id]),
                        "created_at": party.created_at.isoformat(),
                    }
                    for party in team_parties
                ],
            },
            reason="Refill" if refill else "Formation",
        )
        await send_proposals(session, project, event, [row.user_id for row in rows], deadline, now)
        _, minimum, _ = formation_sizes(event, await formation_row(session, event.id), settings)
        if len(await members(session, project.id)) >= minimum:
            await resolve_alerts(session, f"team_below_min:{project.id}", now)
    for party in parties:
        if party.id in placed_parties:
            await session.delete(party)
    await session.flush()


async def send_proposals(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    user_ids: list[UUID],
    deadline: datetime,
    now: datetime,
) -> None:
    rows = await members(session, project.id)
    people = tuple(
        notification_person(user)
        for user in await session.scalars(
            select(models.User)
            .where(models.User.id.in_([row.user_id for row in rows]))
            .order_by(models.User.id)
        )
    )
    for member in rows:
        if member.user_id not in user_ids:
            continue
        await notify(
            session,
            member.user_id,
            TeamProposedPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                project_member_id=member.id,
                accept_by=deadline,
                timezone=event.timezone,
                members=people,
                proposal_reason="Matchmaking placement",
                actions=(
                    ApiAction(
                        key=ActionKey.PLACEMENT_ACCEPT,
                        entity_id=project.id,
                        label_key="notifications.actions.accept",
                    ),
                    ApiAction(
                        key=ActionKey.PLACEMENT_DECLINE,
                        entity_id=project.id,
                        label_key="notifications.actions.decline",
                    ),
                ),
            ),
            now,
            dedupe_key=f"team_proposed:{member.id}",
        )


async def start_run(
    session: AsyncSession, event_id: UUID, version: str, now: datetime, run_id: UUID | None = None
) -> tuple[models.FormationRun, FormationRequest, list[Unit], Packing] | None:
    event = await locked_formation_event(session, event_id)
    formation = await formation_row(session, event_id)
    if formation.phase == "forming" and event.status == "published":
        current = (
            await session.get(models.FormationRun, formation.last_run_id)
            if formation.last_run_id
            else None
        )
        if (
            current is None
            or current.status != "running"
            or (run_id is not None and current.id != run_id)
        ):
            return None
        if isinstance(current.input, dict):
            request = FormationRequest.model_validate(current.input["request"])
            units = (
                [Unit.model_validate(unit) for unit in current.input["units"]]
                if isinstance(current.input["units"], list)
                else []
            )
            return (
                current,
                request,
                units,
                pack(units, [], request.target, request.min_size, request.max_size),
            )
    if formation.phase != "open" or event.status != "published":
        return None
    units, grouped, parties = await queue_units(session, event_id)
    request = await build_formation_request(session, event, units, grouped)
    baseline = pack(units, [], request.target, request.min_size, request.max_size)
    run = await session.get(models.FormationRun, run_id) if run_id else None
    if run is None:
        run = models.FormationRun(
            event_id=event_id, planner="algo", algo_version=version, status="running", input={}
        )
        session.add(run)
    run.status = "running"
    run.algo_version = version
    run.input = {
        "request": request.model_dump(mode="json"),
        "units": [unit.model_dump(mode="json") for unit in units],
        "closed_parties": [str(party.id) for party in parties if party.closed],
    }
    formation.phase = "forming"
    await session.flush()
    formation.last_run_id = run.id
    for invite in await session.scalars(
        select(models.Invite).where(
            models.Invite.event_id == event_id,
            models.Invite.kind == "party",
            models.Invite.status == "pending",
        )
    ):
        await close_invite(session, invite, event, now, "closed", "formation")
    await audit(session, event_id, now, "forming")
    await session.flush()
    await session.refresh(run)
    return run, request, units, baseline


async def plan_formation(
    runtime: Runtime, request: FormationRequest, units: list[Unit], baseline: Packing
) -> tuple[Packing, list[str], JsonValue]:
    if not units:
        return baseline, [], None
    try:
        async with runtime.sessionmaker.begin() as session:
            ctx = await runtime.algo_context(session)
        plan = await call_algo("form_teams", runtime.algorithm.form_teams(ctx, request))
        errors = validate_plan(plan, units, baseline, request.min_size, request.max_size)
        if not errors:
            placed = {unit_id for team in plan.teams for unit_id in team.units}
            return (
                Packing(
                    fills={},
                    new_teams=[team.units for team in plan.teams],
                    queue=[unit.id for unit in units if unit.id not in placed],
                ),
                [],
                plan.model_dump(mode="json"),
            )
        return baseline, errors, plan.model_dump(mode="json")
    except Exception as error:
        return baseline, [f"{type(error).__name__}: {error}"], None


async def apply_run(
    session: AsyncSession,
    event_id: UUID,
    run_id: UUID,
    packing: Packing,
    errors: list[str],
    algo_output: JsonValue,
    now: datetime,
) -> None:
    event = await locked_formation_event(session, event_id)
    formation = await formation_row(session, event_id)
    run = await session.get(models.FormationRun, run_id)
    if (
        run is None
        or run.status != "running"
        or formation.phase != "forming"
        or formation.last_run_id != run.id
        or event.status != "published"
    ):
        return None
    _, grouped, parties = await queue_units(session, event_id)
    _, minimum, _ = formation_sizes(event, formation, await load_settings(session))
    closed = [
        [party.id]
        for party in parties
        if party.closed and len(grouped.get(party.id, [])) >= minimum
    ]
    applied = packing.model_copy(update={"new_teams": closed + packing.new_teams})
    await apply_packing(session, event, applied, grouped, parties, now)
    reserve = [row for group in grouped.values() for row in group if row.status != "placed"]
    for row in reserve:
        row.status = "reserve"
    if reserve:
        await notify_many(
            session,
            [row.user_id for row in reserve],
            ReservePlacedPayload(
                link=f"/events/{event_id}/formation",
                event_id=event_id,
                event_title=event.title,
                reserve_count=len(reserve),
            ),
            now,
            dedupe_key=f"reserve_placed:{run.id}",
        )
    formation.phase = "formed"
    run.status = "applied"
    run.planner = "fallback" if errors else "algo"
    run.output = {"packing": applied.model_dump(mode="json"), "algo": algo_output}
    run.validation_errors = list(errors)
    run.applied_at = now
    await session.flush()
    await audit(session, event_id, now, "formed")
    if errors:
        await formation_alert(session, event, now, "; ".join(errors))


async def formation_alert(
    session: AsyncSession, event: models.Event, now: datetime, message: str
) -> None:
    await raise_alert(
        session,
        AlertKind.FORMATION_FAILED,
        now,
        title=f"Formation failed: {event.title}",
        message=message,
        link=f"/admin/events/{event.id}/formation",
        subject_type="event",
        subject_id=event.id,
        subject_title=event.title,
        dedupe_key=f"formation_failed:{event.id}",
    )


async def fail_run(
    session: AsyncSession, event_id: UUID, run_id: UUID | None, error: Exception, now: datetime
) -> None:
    event = await locked_formation_event(session, event_id)
    formation = await formation_row(session, event_id)
    if formation.phase not in {"open", "forming"} or event.status != "published":
        return None
    if formation.phase == "forming" and formation.last_run_id != run_id:
        return None
    run = await session.get(models.FormationRun, run_id) if run_id else None
    if run is not None:
        run.status = "failed"
        run.validation_errors = [f"{type(error).__name__}: {error}"]
    else:
        run = models.FormationRun(
            event_id=event_id,
            status="failed",
            planner="fallback",
            input={},
            validation_errors=[f"{type(error).__name__}: {error}"],
            algo_version="",
        )
        session.add(run)
        await session.flush()
    formation.last_run_id = run.id
    formation.phase = "open"
    formation.formation_at = now + timedelta(hours=1)
    await session.flush()
    await audit(session, event_id, now, "open", reason=str(error))
    await formation_alert(session, event, now, str(error))


async def request_formation_run(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> schemas.FormationRun:
    event, formation = await participation_event(session, event_id)
    if formation.phase != "open" or event.status != "published":
        raise ProblemError(ErrorCode.INVALID_STATE)
    run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.event_id == event_id, models.FormationRun.status == "pending")
        .limit(1)
    )
    if run is None:
        run = models.FormationRun(
            event_id=event_id,
            status="pending",
            planner="algo",
            input={},
            validation_errors=[],
            algo_version="",
        )
        session.add(run)
        await session.flush()
        await defer_job(
            session, jobs, JobName.RUN_FORMATION, event_id=str(event_id), run_id=str(run.id)
        )
        await audit(
            session,
            event_id,
            now,
            "pending",
            actor,
            AuditAction.FORMATION_RUN,
            body.reason if body else None,
        )
    await session.refresh(run)
    return schemas.FormationRun.model_validate(run, from_attributes=True)


async def update_formation(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateFormationBody,
    actor: models.User,
    now: datetime,
) -> schemas.Formation:
    event, formation = await participation_event(session, event_id)
    if formation.phase != "open":
        raise ProblemError(ErrorCode.INVALID_STATE)
    settings = await load_settings(session)
    _, minimum, maximum = formation_sizes(event, formation, settings)
    if "target_override" in body.model_fields_set:
        if body.target_override is not None and not minimum <= body.target_override <= maximum:
            raise ProblemError(ErrorCode.VALIDATION_ERROR)
        formation.target_override = body.target_override
    if "formation_at_override" in body.model_fields_set:
        formation.formation_at_override = body.formation_at_override
    formation.formation_at = formation_time(
        event, settings, formation.created_at, formation.formation_at_override
    )
    await session.flush()
    await session.refresh(formation)
    await audit(
        session, event_id, now, "settings_updated", actor, AuditAction.FORMATION_EDIT, body.reason
    )
    view = await formation_participation(session, event, actor, now)
    if view is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return view


async def list_formation_runs(session: AsyncSession, event_id: UUID) -> list[schemas.FormationRun]:
    return [
        schemas.FormationRun.model_validate(row, from_attributes=True)
        for row in await session.scalars(
            select(models.FormationRun)
            .where(models.FormationRun.event_id == event_id)
            .order_by(models.FormationRun.created_at.desc(), models.FormationRun.id.desc())
            .limit(50)
        )
    ]


async def refill_event(session: AsyncSession, event: models.Event, now: datetime) -> None:
    event, formation = await participation_event(session, event.id)
    if formation.phase != "formed" or event.status != "published" or now >= reg_deadline(event):
        return None
    target, minimum, maximum = formation_sizes(event, formation, await load_settings(session))
    units, grouped, parties = await queue_units(session, event.id)
    packing = pack(
        units, await open_teams(session, event.id, target, grouped), target, minimum, maximum
    )
    packing = packing.model_copy(
        update={
            "new_teams": [
                [party.id]
                for party in parties
                if party.closed and len(grouped.get(party.id, [])) >= minimum
            ]
            + packing.new_teams
        }
    )
    await apply_packing(session, event, packing, grouped, parties, now, refill=True)
