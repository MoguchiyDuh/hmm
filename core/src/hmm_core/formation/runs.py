from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.algo import AlgoSettings, FormationPlan, FormationRequest
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    ErrorCode,
    EventStatus,
    FormationKind,
    FormationPhase,
    FormationPlanner,
    FormationRunStatus,
    LeftReason,
    MemberState,
)
from hmm_contracts.notifications import AlertKind, FormationRescheduledPayload
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.formation.apply import dissolve_core
from hmm_core.formation.common import (
    FORMATION_ALGO_LIMIT,
    LIVE_ENROLLMENT_STATES,
    formation_event,
    formation_timing,
    live_enrollment_row,
    locked_formation_event,
)
from hmm_core.formation.enrollments import formation_participation
from hmm_core.formation.lead_offers import close_pending_lead_offers
from hmm_core.formation.leads import close_project_pending_items, formation_sizes
from hmm_core.formation.planning import (
    build_formation_request,
    fallback_plan,
    preserved_projects,
    validate_plan,
)
from hmm_core.notifications.service import notify_many, raise_alert
from hmm_core.platform.algo import call_algo
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, Runtime, defer_job
from hmm_core.platform.schemas import ReasonBody
from hmm_core.platform.settings import load_settings


async def open_formation_run(
    session: AsyncSession,
    event_id: UUID,
    kind: FormationKind,
    algo_version: str,
    now: datetime,
) -> tuple[models.FormationRun, bool]:
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if event.status != EventStatus.PUBLISHED.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation is available for published hackathons"
        )
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    allowed = (
        {FormationPhase.LOCKED}
        if kind == FormationKind.LATE
        else {FormationPhase.OPEN, FormationPhase.REVIEW}
    )
    if phase not in allowed:
        raise ProblemError(ErrorCode.INVALID_STATE, "Formation cannot run in this phase")
    existing = await session.scalar(
        select(models.FormationRun)
        .where(
            models.FormationRun.event_id == event.id,
            models.FormationRun.kind == kind.value,
            models.FormationRun.status.in_(
                [FormationRunStatus.PENDING.value, FormationRunStatus.RUNNING.value]
            ),
        )
        .order_by(models.FormationRun.created_at.desc(), models.FormationRun.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing, False
    run = models.FormationRun(
        event_id=event.id,
        round=formation.rounds + 1,
        kind=kind.value,
        status=FormationRunStatus.PENDING.value,
        planner=FormationPlanner.ALGO.value,
        input={},
        output=None,
        validation_errors=[],
        algo_version=algo_version,
    )
    session.add(run)
    await session.flush()
    return run, True


async def start_formation_run(
    session: AsyncSession,
    event_id: UUID,
    kind: FormationKind,
    run_id: UUID,
    algo_version: str,
    now: datetime,
) -> tuple[models.FormationRun, FormationRequest] | None:
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    formation = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or formation is None:
        return None
    run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.event_id != event.id:
        return None
    if run.status == FormationRunStatus.APPLIED.value:
        return None
    run.round = formation.rounds + 1
    phase = FormationPhase(formation.phase)
    allowed = (
        {FormationPhase.LOCKED, FormationPhase.FORMING}
        if kind == FormationKind.LATE
        else {FormationPhase.OPEN, FormationPhase.REVIEW, FormationPhase.FORMING}
    )
    if phase not in allowed:
        run.status = FormationRunStatus.FAILED.value
        run.validation_errors = [
            {
                "attempt": 0,
                "planner": FormationPlanner.ALGO.value,
                "plan": None,
                "errors": [],
                "error": f"Formation cannot start in phase {phase}",
            }
        ]
        run.planner = FormationPlanner.ALGO.value
        await session.flush()
        return None
    if phase != FormationPhase.FORMING:
        pre_phase = phase
    elif kind == FormationKind.LATE:
        pre_phase = FormationPhase.LOCKED
    elif formation.rounds > 0:
        pre_phase = FormationPhase.REVIEW
    else:
        pre_phase = FormationPhase.OPEN
    rerun = kind == FormationKind.MAIN and pre_phase == FormationPhase.REVIEW
    scoped = await preserved_projects(session, event.id, kind, pre_phase)
    for project in scoped:
        await close_project_pending_items(session, project.id, now, reason="formation")
        await close_pending_lead_offers(session, project.id, now)
    if rerun:
        for project in scoped:
            proposed = await session.scalars(
                select(models.ProjectMember).where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                    models.ProjectMember.state == MemberState.PROPOSED.value,
                )
            )
            for row in proposed:
                row.left_at = now
                row.left_reason = LeftReason.REMOVED.value
                enrollment = await live_enrollment_row(
                    session, event.id, row.user_id, for_update=True
                )
                if enrollment is not None and enrollment.status == EnrollmentStatus.PLACED.value:
                    enrollment.status = EnrollmentStatus.ACTIVE.value
                    enrollment.status_at = now
                    enrollment.reserve_rank = None
        await session.flush()
    for project in scoped:
        live_ids = set(
            await session.scalars(
                select(models.ProjectMember.user_id)
                .join(models.User, models.User.id == models.ProjectMember.user_id)
                .where(
                    models.ProjectMember.project_id == project.id,
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                    models.User.banned_at.is_(None),
                )
            )
        )
        if not live_ids:
            await dissolve_core(
                session,
                event,
                project,
                now,
                reason="No members remained after proposals were released",
            )
        elif (project.lead_user_id is None or project.lead_user_id not in live_ids) and not rerun:
            await dissolve_core(
                session,
                event,
                project,
                now,
                reason="A core without a lead was dissolved at formation",
            )
    request = await build_formation_request(session, event, formation, kind, pre_phase, now)
    formation.phase = FormationPhase.FORMING.value
    run.input = request.model_dump(mode="json")
    run.status = FormationRunStatus.RUNNING.value
    run.validation_errors = []
    run.algo_version = algo_version
    run.round = formation.rounds + 1
    await session.flush()
    return run, request


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    plan: FormationPlan | None
    planner: FormationPlanner
    attempts: list[JsonValue]
    error: str | None


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _attempt(
    attempt: int,
    planner: FormationPlanner,
    plan: FormationPlan | None,
    errors: list[JsonValue],
    error: str | None,
) -> JsonValue:
    return {
        "attempt": attempt,
        "planner": planner.value,
        "plan": plan.model_dump(mode="json") if plan is not None else None,
        "errors": errors,
        "error": error,
    }


def failure_outcome(error: Exception) -> PlanOutcome:
    text = _error_text(error)
    return PlanOutcome(
        plan=None,
        planner=FormationPlanner.ALGO,
        attempts=[_attempt(1, FormationPlanner.ALGO, None, [], text)],
        error=text,
    )


def _fallback_outcome(
    request: FormationRequest, settings: AlgoSettings, attempt: int, attempts: list[JsonValue]
) -> PlanOutcome:
    plan = fallback_plan(request, settings)
    errors = validate_plan(request, plan, settings)
    attempts.append(_attempt(attempt, FormationPlanner.FALLBACK, plan, errors, None))
    return PlanOutcome(
        plan=plan if not errors else None,
        planner=FormationPlanner.FALLBACK,
        attempts=attempts,
        error=None if not errors else "Fallback plan is invalid",
    )


async def plan_formation(runtime: Runtime, request: FormationRequest) -> PlanOutcome:
    async with runtime.sessionmaker.begin() as session:
        ctx = await runtime.algo_context(session)
    settings = ctx.settings
    attempts: list[JsonValue] = []
    if len(request.unplaced) > FORMATION_ALGO_LIMIT:
        return _fallback_outcome(request, settings, 1, attempts)
    for attempt in (1, 2):
        plan: FormationPlan | None
        errors: list[JsonValue]
        error: str | None = None
        try:
            plan = await call_algo("form_teams", runtime.algorithm.form_teams(ctx, request))
            errors = validate_plan(request, plan, settings)
        except Exception as failure:
            plan, errors, error = None, [], _error_text(failure)
        attempts.append(_attempt(attempt, FormationPlanner.ALGO, plan, errors, error))
        if plan is not None and error is None and not errors:
            return PlanOutcome(
                plan=plan, planner=FormationPlanner.ALGO, attempts=attempts, error=None
            )
    return _fallback_outcome(request, settings, 3, attempts)


async def fail_formation_run(
    session: AsyncSession, run_id: UUID, outcome: PlanOutcome, now: datetime
) -> None:
    run = await session.scalar(
        select(models.FormationRun)
        .where(models.FormationRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or run.status in {
        FormationRunStatus.APPLIED.value,
        FormationRunStatus.FAILED.value,
    }:
        return None
    event = await session.scalar(
        select(models.Event)
        .where(models.Event.id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    formation = await session.scalar(
        select(models.EventFormation)
        .where(models.EventFormation.event_id == run.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or formation is None:
        return None
    run.status = FormationRunStatus.FAILED.value
    run.output = None
    run.validation_errors = outcome.attempts
    run.planner = outcome.planner.value
    run.applied_at = None
    if FormationKind(run.kind) == FormationKind.LATE:
        formation.phase = FormationPhase.LOCKED.value
    elif formation.rounds > 0:
        formation.phase = FormationPhase.REVIEW.value
    else:
        settings = await load_settings(session)
        formation.phase = FormationPhase.OPEN.value
        formation.formation_at = now + timedelta(hours=1)
        formation.review_ends_at = min(
            formation.formation_at + timedelta(hours=settings.review_hours),
            formation_timing(event, settings, now).team_deadline,
        )
    await session.flush()
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.FORMATION,
        entity_id=event.id,
        before={"run_id": str(run.id), "phase": FormationPhase.FORMING.value},
        after={
            "phase": formation.phase,
            "planner": outcome.planner.value,
            "error": outcome.error,
        },
        reason=None,
    )
    await raise_alert(
        session,
        AlertKind.FORMATION_FAILED,
        now,
        title=f"Formation failed: {event.title}",
        message=outcome.error or "Formation could not build a valid plan",
        link=f"/admin/events/{event.id}/formation",
        subject_type="event",
        subject_id=event.id,
        subject_title=event.title,
        dedupe_key=f"formation_failed:{event.id}:{run.id}",
    )
    await session.flush()
    return None


async def update_formation(
    session: AsyncSession,
    event_id: UUID,
    body: schemas.UpdateFormationBody,
    actor: models.User,
    now: datetime,
) -> schemas.Formation:
    event = await locked_formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await session.get(models.EventFormation, event.id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    phase = FormationPhase(formation.phase)
    if phase == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    if phase != FormationPhase.OPEN:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation settings are frozen once formation starts"
        )
    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    changes = body.model_dump(exclude_unset=True)
    target = changes.get("target_override", formation.target_override)
    if target is not None and not sizes.min_size <= target <= sizes.max_size:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR,
            f"target_override must be between {sizes.min_size} and {sizes.max_size}",
        )
    override = changes.get("formation_at_override", formation.formation_at_override)
    if override is not None and override <= now:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR, "formation_at_override must be in the future"
        )
    before: dict[str, JsonValue] = {
        "target_override": formation.target_override,
        "formation_at_override": formation.formation_at_override.isoformat()
        if formation.formation_at_override is not None
        else None,
        "formation_at": formation.formation_at.isoformat(),
        "review_ends_at": formation.review_ends_at.isoformat()
        if formation.review_ends_at is not None
        else None,
    }
    previous = formation.formation_at
    if "target_override" in changes:
        formation.target_override = changes["target_override"]
    if "formation_at_override" in changes:
        formation.formation_at_override = changes["formation_at_override"]
        timing = formation_timing(event, settings, now)
        candidate = formation.formation_at_override or timing.formation_at
        if candidate != formation.formation_at:
            formation.formation_at = candidate
            formation.review_ends_at = min(
                candidate + timedelta(hours=settings.review_hours), timing.team_deadline
            )
    after: dict[str, JsonValue] = {
        "target_override": formation.target_override,
        "formation_at_override": formation.formation_at_override.isoformat()
        if formation.formation_at_override is not None
        else None,
        "formation_at": formation.formation_at.isoformat(),
        "review_ends_at": formation.review_ends_at.isoformat()
        if formation.review_ends_at is not None
        else None,
    }
    if after != before:
        await session.flush()
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.ADMIN,
            action=AuditAction.FORMATION_EDIT,
            entity_type=AuditEntityType.FORMATION,
            entity_id=event.id,
            before=before,
            after=after,
            reason=body.reason,
        )
        if formation.formation_at != previous and abs(
            formation.formation_at - previous
        ) > timedelta(hours=6):
            enrolled = await session.scalars(
                select(models.Enrollment.user_id)
                .where(
                    models.Enrollment.event_id == event.id,
                    models.Enrollment.status.in_(
                        sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                    ),
                )
                .order_by(models.Enrollment.created_at, models.Enrollment.id)
            )
            leads = await session.scalars(
                select(models.Project.lead_user_id)
                .where(
                    models.Project.event_id == event.id,
                    models.Project.lead_user_id.is_not(None),
                )
                .order_by(models.Project.created_at, models.Project.id)
            )
            ids = [*enrolled, *(lead for lead in leads if lead is not None)]
            await notify_many(
                session,
                ids,
                FormationRescheduledPayload(
                    link=f"/events/{event.id}/formation",
                    event_id=event.id,
                    event_title=event.title,
                    previous_formation_at=previous,
                    formation_at=formation.formation_at,
                    timezone=event.timezone,
                    reason=body.reason or "Admin changed the formation schedule",
                ),
                now,
                dedupe_key=f"formation_rescheduled:{event.id}:{formation.formation_at.isoformat()}",
            )
        await session.flush()
    view = await formation_participation(session, event, actor, now)
    if view is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return view


async def request_formation_run(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> models.FormationRun:
    run, created = await open_formation_run(session, event_id, FormationKind.MAIN, "", now)
    if not created:
        return run
    formation = await session.get(models.EventFormation, event_id)
    if formation is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.FORMATION_RUN,
        entity_type=AuditEntityType.FORMATION,
        entity_id=event_id,
        before={"phase": formation.phase, "rounds": formation.rounds},
        after={"run_id": str(run.id), "kind": FormationKind.MAIN.value, "round": run.round},
        reason=body.reason if body is not None else None,
    )
    await defer_job(
        session,
        jobs,
        JobName.RUN_FORMATION,
        event_id=str(event_id),
        run_id=str(run.id),
        kind=FormationKind.MAIN.value,
    )
    await session.refresh(run)
    return run


def formation_run_view(row: models.FormationRun) -> schemas.FormationRun:
    return schemas.FormationRun(
        id=row.id,
        event_id=row.event_id,
        round=row.round,
        kind=FormationKind(row.kind),
        status=FormationRunStatus(row.status),
        planner=FormationPlanner(row.planner) if row.planner else None,
        input=FormationRequest.model_validate(row.input)
        if isinstance(row.input, dict) and row.input
        else None,
        output=FormationPlan.model_validate(row.output)
        if isinstance(row.output, dict) and row.output
        else None,
        validation_errors=row.validation_errors if isinstance(row.validation_errors, list) else [],
        algo_version=row.algo_version or None,
        created_at=row.created_at,
        applied_at=row.applied_at,
    )


async def list_formation_runs(
    session: AsyncSession, event_id: UUID, actor: models.User
) -> list[schemas.FormationRun]:
    event = await formation_event(session, event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    rows = await session.scalars(
        select(models.FormationRun)
        .where(models.FormationRun.event_id == event.id)
        .order_by(models.FormationRun.created_at.desc(), models.FormationRun.id.desc())
        .limit(50)
    )
    return [formation_run_view(row) for row in rows]
