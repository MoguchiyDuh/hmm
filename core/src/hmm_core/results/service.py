from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    MemberState,
    PlaceStatus,
    ProjectStatus,
    ResultOutcome,
    ReviewWindowSubjectType,
    SourcePersonRole,
    TeamStatus,
)
from pydantic import JsonValue
from sqlalchemy import ColumnElement, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance.service import ensure_attended
from hmm_core.feed import service as feed
from hmm_core.notifications.service import resolve_alerts
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.settings import load_settings
from hmm_core.projects.service import project_ref
from hmm_core.results import schemas
from hmm_core.results.schemas import ProjectResult as ProjectResultView
from hmm_core.results.schemas import ResultAction
from hmm_core.results.windows import open_review_window, result_review_participants


async def load_project(session: AsyncSession, project_id: UUID) -> models.Project:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


async def lock_project(session: AsyncSession, project_id: UUID) -> models.Project:
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


def _team_filter(project_id: UUID) -> tuple[ColumnElement[bool], ...]:
    return (
        models.ProjectMember.project_id == project_id,
        models.ProjectMember.state == MemberState.CONFIRMED,
        models.ProjectMember.left_at.is_(None),
        models.ProjectMember.is_team_row.is_(True),
    )


async def team_member_ids(session: AsyncSession, project_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.ProjectMember.user_id)
            .where(*_team_filter(project_id))
            .order_by(models.ProjectMember.user_id)
        )
    )


async def team_member_rows(
    session: AsyncSession, project_id: UUID
) -> dict[UUID, models.ProjectMember]:
    rows = await session.scalars(select(models.ProjectMember).where(*_team_filter(project_id)))
    return {row.user_id: row for row in rows}


async def open_result_window(
    session: AsyncSession, project_id: UUID, now: datetime
) -> models.ReviewWindow | None:
    return await session.scalar(
        select(models.ReviewWindow)
        .where(
            models.ReviewWindow.subject_type == ReviewWindowSubjectType.PROJECT,
            models.ReviewWindow.subject_id == project_id,
            models.ReviewWindow.revealed_at.is_(None),
            models.ReviewWindow.opens_at <= now,
            models.ReviewWindow.closes_at > now,
        )
        .order_by(models.ReviewWindow.closes_at.desc())
        .limit(1)
    )


async def winner_place(session: AsyncSession, event_id: UUID, user_ids: list[UUID]) -> int | None:
    if not user_ids:
        return None
    return await session.scalar(
        select(func.min(models.EventSourcePerson.place)).where(
            models.EventSourcePerson.event_id == event_id,
            models.EventSourcePerson.role == SourcePersonRole.WINNER.value,
            models.EventSourcePerson.place.is_not(None),
            models.EventSourcePerson.matched_user_id.in_(user_ids),
        )
    )


async def validate_team_lists(
    session: AsyncSession,
    *,
    team_ids: list[UUID],
    participants: dict[UUID, bool],
    member_areas: dict[UUID, str],
) -> None:
    if set(participants) != set(team_ids):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "participants must cover every team member")
    if set(member_areas) != set(team_ids):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "member_areas must cover every team member")
    keys = set(member_areas.values())
    known = set(
        await session.scalars(
            select(models.Area.key).where(
                models.Area.key.in_(keys), models.Area.archived_at.is_(None)
            )
        )
    )
    if known != keys:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Unknown area")


async def ensure_result_release(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    result: models.ProjectResult,
    *,
    actor_id: UUID,
    now: datetime,
) -> models.ProjectRelease | None:
    title = f"Hackathon result: {event.title}"[:200]
    release = await session.scalar(
        select(models.ProjectRelease)
        .where(models.ProjectRelease.project_id == project.id, models.ProjectRelease.title == title)
        .with_for_update()
    )
    if project.status != ProjectStatus.SHIPPED.value:
        return release
    if release is None:
        release = models.ProjectRelease(
            project_id=project.id,
            title=title,
            notes_md=result.award or "",
            repo_url=result.repo_url,
            demo_url=result.demo_url,
            writeup_url=result.presentation_url,
            created_by=result.submitted_by or actor_id,
        )
        session.add(release)
    else:
        release.notes_md = result.award or ""
        release.repo_url = result.repo_url
        release.demo_url = result.demo_url
        release.writeup_url = result.presentation_url
    await session.flush()
    return release


async def apply_result_effects(
    session: AsyncSession,
    jobs: procrastinate.App,
    project: models.Project,
    event: models.Event,
    result: models.ProjectResult,
    *,
    actor_id: UUID,
    now: datetime,
) -> None:
    if result.outcome == ResultOutcome.FINISHED.value:
        project.team_status = TeamStatus.FINISHED.value
        project.status = (
            ProjectStatus.SHIPPED.value
            if result.repo_url or result.demo_url
            else ProjectStatus.ABANDONED.value
        )
        await session.flush()
        release = await ensure_result_release(
            session, project, event, result, actor_id=actor_id, now=now
        )
        members = await team_member_rows(session, project.id)
        for key, value in cast(dict[str, bool], result.participants).items():
            uid = UUID(str(key))
            if value is not True or uid not in members:
                continue
            await feed.add_confirmation(
                session,
                jobs,
                fact_type=ConfirmationFactType.PROJECT_MEMBER,
                fact_id=members[uid].id,
                subject_user_id=uid,
                confirmer_id=None,
                source=ConfirmationSource.SYSTEM,
                now=now,
            )
            await ensure_attended(session, jobs, event_id=event.id, user_id=uid, now=now)
        await feed.record_project_shipped(session, project, release, actor_id, now)
        window_exists = await session.scalar(
            select(
                exists().where(
                    models.ReviewWindow.subject_type == ReviewWindowSubjectType.PROJECT,
                    models.ReviewWindow.subject_id == project.id,
                )
            )
        )
        if not window_exists:
            await open_review_window(
                session,
                subject_type=ReviewWindowSubjectType.PROJECT,
                subject_id=project.id,
                participants=await result_review_participants(session, project_id=project.id),
                now=now,
            )
    else:
        project.team_status = TeamStatus.WITHDRAWN.value
        project.status = ProjectStatus.ABANDONED.value
    for user_id in await team_member_ids(session, project.id):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))


async def build_result(
    session: AsyncSession,
    result: models.ProjectResult,
    project: models.Project,
    actor: models.User,
    now: datetime,
    *,
    admin: bool,
) -> ProjectResultView:
    window = await open_result_window(session, project.id, now)
    on_team = (
        await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.state == MemberState.CONFIRMED,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
        )
        is not None
    )
    actions: list[ResultAction] = []
    if admin or (
        result.outcome == ResultOutcome.FINISHED.value
        and actor.id in {result.submitted_by, project.lead_user_id}
        and window is not None
    ):
        actions.append(ResultAction.EDIT)
    if admin and result.place_status == PlaceStatus.UNVERIFIED.value:
        actions.append(ResultAction.VERIFY)
    return ProjectResultView.model_validate(
        {
            "project_id": result.project_id,
            "outcome": ResultOutcome(result.outcome),
            "place": result.place,
            "award": result.award,
            "place_status": PlaceStatus(result.place_status)
            if result.place_status is not None
            else None,
            "place_verified_by": result.place_verified_by,
            "place_verified_at": result.place_verified_at,
            "repo_url": result.repo_url,
            "demo_url": result.demo_url,
            "presentation_url": result.presentation_url,
            "retrospective_md": result.retrospective_md if admin or on_team else None,
            "member_areas": result.member_areas,
            "participants": result.participants,
            "auto_closed": result.auto_closed,
            "submitted_by": result.submitted_by,
            "submitted_at": result.submitted_at,
            "edited_at": result.edited_at,
            "allowed_actions": actions,
        }
    )


async def submit_result(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.CreateResultBody,
    actor: models.User,
    now: datetime,
    *,
    admin: bool,
) -> ProjectResultView:
    project = await lock_project(session, project_id)
    if project.team_status != TeamStatus.ACTIVE.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Team is not active")
    event = await session.get(models.Event, project.event_id) if project.event_id else None
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if now < event.ends_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "Event has not ended")
    if await session.get(models.ProjectResult, project_id) is not None:
        raise ProblemError(ErrorCode.RESULT_EXISTS)
    team_ids = await team_member_ids(session, project_id)
    if not admin and actor.id != project.lead_user_id:
        if actor.id not in team_ids:
            raise ProblemError(ErrorCode.FORBIDDEN)
        due_days = (await load_settings(session)).results_due_days
        if now < event.ends_at + timedelta(days=due_days):
            raise ProblemError(ErrorCode.RESULT_NOT_DUE)
    await validate_team_lists(
        session, team_ids=team_ids, participants=body.participants, member_areas=body.member_areas
    )
    claimed = body.place is not None or body.award is not None
    place = body.place
    place_status = PlaceStatus.UNVERIFIED.value if claimed else None
    if claimed:
        found = await winner_place(session, event.id, team_ids)
        if found is not None:
            place = found
            place_status = PlaceStatus.VERIFIED.value
    result = models.ProjectResult(
        project_id=project_id,
        outcome=body.outcome.value,
        place=place,
        award=body.award,
        place_status=place_status,
        place_verified_by=None,
        place_verified_at=now if place_status == PlaceStatus.VERIFIED.value else None,
        repo_url=str(body.repo_url) if body.repo_url else None,
        demo_url=str(body.demo_url) if body.demo_url else None,
        presentation_url=str(body.presentation_url) if body.presentation_url else None,
        retrospective_md=body.retrospective_md,
        member_areas={str(key): value for key, value in body.member_areas.items()},
        participants={str(key): value for key, value in body.participants.items()},
        auto_closed=False,
        submitted_by=actor.id,
        submitted_at=now,
        edited_at=None,
    )
    session.add(result)
    await session.flush()
    await apply_result_effects(session, jobs, project, event, result, actor_id=actor.id, now=now)
    await resolve_alerts(session, f"result_overdue:{project_id}", now, note="Result submitted")
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN if admin else ActorKind.USER,
        action=AuditAction.RESULT_SUBMIT,
        entity_type=AuditEntityType.PROJECT_RESULT,
        entity_id=project_id,
        before=None,
        after={
            "outcome": result.outcome,
            "place": result.place,
            "award": result.award,
            "place_status": result.place_status,
            "auto_closed": False,
        },
    )
    await session.flush()
    return await build_result(session, result, project, actor, now, admin=admin)


async def fetch_result(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime, *, admin: bool
) -> ProjectResultView:
    project = await load_project(session, project_id)
    result = await session.get(models.ProjectResult, project_id)
    if result is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return await build_result(session, result, project, actor, now, admin=admin)


async def _delete_system_confirmations(
    session: AsyncSession, fact_type: ConfirmationFactType, fact_id: UUID
) -> list[UUID]:
    return list(
        await session.scalars(
            delete(models.Confirmation)
            .where(
                models.Confirmation.fact_type == fact_type.value,
                models.Confirmation.fact_id == fact_id,
                models.Confirmation.confirmer_id.is_(None),
                models.Confirmation.source == ConfirmationSource.SYSTEM.value,
                models.Confirmation.withdrawn_at.is_(None),
            )
            .returning(models.Confirmation.id)
        )
    )


async def remove_result_confirmations(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    event: models.Event,
    user_id: UUID,
    member: models.ProjectMember | None,
    now: datetime,
) -> None:
    if member is not None and await _delete_system_confirmations(
        session, ConfirmationFactType.PROJECT_MEMBER, member.id
    ):
        await feed.recompute_fact_trust(
            session,
            jobs,
            fact_type=ConfirmationFactType.PROJECT_MEMBER,
            fact_id=member.id,
            subject_user_id=user_id,
            now=now,
        )
    attendance = await session.scalar(
        select(models.EventAttendance).where(
            models.EventAttendance.event_id == event.id,
            models.EventAttendance.user_id == user_id,
        )
    )
    if attendance is not None and await _delete_system_confirmations(
        session, ConfirmationFactType.ATTENDANCE, attendance.id
    ):
        await feed.recompute_fact_trust(
            session,
            jobs,
            fact_type=ConfirmationFactType.ATTENDANCE,
            fact_id=attendance.id,
            subject_user_id=user_id,
            now=now,
        )


def _result_snapshot(result: models.ProjectResult) -> dict[str, JsonValue]:
    return {
        "outcome": result.outcome,
        "place": result.place,
        "award": result.award,
        "place_status": result.place_status,
        "repo_url": result.repo_url,
        "demo_url": result.demo_url,
        "presentation_url": result.presentation_url,
        "retrospective_md": result.retrospective_md,
        "participants": result.participants,
        "member_areas": result.member_areas,
    }


async def update_result(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.UpdateResultBody,
    actor: models.User,
    now: datetime,
    *,
    admin: bool,
) -> ProjectResultView:
    project = await lock_project(session, project_id)
    result = await session.scalar(
        select(models.ProjectResult)
        .where(models.ProjectResult.project_id == project_id)
        .with_for_update()
    )
    if result is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await session.get(models.Event, project.event_id) if project.event_id else None
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    window = await open_result_window(session, project_id, now)
    if not admin:
        if actor.id not in {result.submitted_by, project.lead_user_id}:
            raise ProblemError(ErrorCode.FORBIDDEN)
        if result.outcome != ResultOutcome.FINISHED.value or window is None:
            raise ProblemError(ErrorCode.INVALID_STATE, "Result is no longer editable")
    team_ids = await team_member_ids(session, project_id)
    members = await team_member_rows(session, project_id)
    fields = body.model_fields_set
    old_participants = {
        UUID(key): bool(value) for key, value in cast(dict[str, bool], result.participants).items()
    }
    old_areas = {
        UUID(key): str(value) for key, value in cast(dict[str, str], result.member_areas).items()
    }
    new_participants = (
        dict(body.participants) if body.participants is not None else dict(old_participants)
    )
    new_areas = dict(body.member_areas) if body.member_areas is not None else dict(old_areas)
    participants_changed = new_participants != old_participants
    outcome_changed = body.outcome is not None and body.outcome.value != result.outcome
    if not admin and (participants_changed or outcome_changed):
        raise ProblemError(
            ErrorCode.INVALID_STATE,
            "Participants and outcome are locked once the review window opens",
        )
    if body.participants is not None or body.member_areas is not None:
        await validate_team_lists(
            session, team_ids=team_ids, participants=new_participants, member_areas=new_areas
        )
    before = _result_snapshot(result)
    if "outcome" in fields and body.outcome is not None:
        result.outcome = body.outcome.value
    if "place" in fields:
        result.place = body.place
    if "award" in fields:
        result.award = body.award
    if "retrospective_md" in fields:
        result.retrospective_md = body.retrospective_md
    if "repo_url" in fields:
        result.repo_url = str(body.repo_url) if body.repo_url is not None else None
    if "demo_url" in fields:
        result.demo_url = str(body.demo_url) if body.demo_url is not None else None
    if "presentation_url" in fields:
        result.presentation_url = (
            str(body.presentation_url) if body.presentation_url is not None else None
        )
    if "member_areas" in fields and body.member_areas is not None:
        result.member_areas = {str(key): value for key, value in body.member_areas.items()}
    if "participants" in fields and body.participants is not None:
        result.participants = {str(key): value for key, value in body.participants.items()}
    if "place" in fields or "award" in fields:
        result.place_verified_by = None
        if result.place is None and result.award is None:
            result.place_status = None
            result.place_verified_at = None
        else:
            found = await winner_place(session, event.id, team_ids)
            if found is not None:
                result.place = found
                result.place_status = PlaceStatus.VERIFIED.value
                result.place_verified_at = now
            else:
                result.place_status = PlaceStatus.UNVERIFIED.value
                result.place_verified_at = None
    result.edited_at = now
    await session.flush()
    if participants_changed:
        removed = {uid for uid, value in old_participants.items() if value is True} - {
            uid for uid, value in new_participants.items() if value is True
        }
        for uid in sorted(removed):
            await remove_result_confirmations(
                session, jobs, event=event, user_id=uid, member=members.get(uid), now=now
            )
    await apply_result_effects(session, jobs, project, event, result, actor_id=actor.id, now=now)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN if admin else ActorKind.USER,
        action=AuditAction.RESULT_EDIT,
        entity_type=AuditEntityType.PROJECT_RESULT,
        entity_id=project_id,
        before=before,
        after=_result_snapshot(result),
    )
    await session.flush()
    return await build_result(session, result, project, actor, now, admin=admin)


def _winner_candidate(row: models.EventSourcePerson) -> schemas.WinnerCandidate:
    return schemas.WinnerCandidate(
        id=row.id,
        source_id=row.source_id,
        github_login=row.github_login,
        telegram_username=row.telegram_username,
        display_name=row.display_name,
        place=row.place,
        matched_user_id=row.matched_user_id,
    )


async def winner_candidates(
    session: AsyncSession, event_id: UUID, user_ids: list[UUID]
) -> list[schemas.WinnerCandidate]:
    if not user_ids:
        return []
    rows = await session.scalars(
        select(models.EventSourcePerson)
        .where(
            models.EventSourcePerson.event_id == event_id,
            models.EventSourcePerson.role == SourcePersonRole.WINNER.value,
            models.EventSourcePerson.matched_user_id.in_(user_ids),
        )
        .order_by(
            models.EventSourcePerson.place.is_(None),
            models.EventSourcePerson.place,
            models.EventSourcePerson.display_name,
            models.EventSourcePerson.id,
        )
    )
    return [_winner_candidate(row) for row in rows]


async def result_verify_page(
    session: AsyncSession,
    params: CursorParams,
    actor: models.User,
    now: datetime,
    *,
    place_status: PlaceStatus | None,
) -> schemas.ResultToVerifyPage:
    statement = select(models.ProjectResult)
    if place_status is not None:
        statement = statement.where(models.ProjectResult.place_status == place_status.value)
    rows = list(
        await session.scalars(
            apply_keyset(statement, params, sort=[], id_column=models.ProjectResult.project_id)
        )
    )
    items = rows[: params.limit]
    next_cursor = encode_cursor([items[-1].project_id]) if len(rows) > params.limit else None
    if not items:
        return schemas.ResultToVerifyPage(items=[], next_cursor=None)
    ids = [result.project_id for result in items]
    projects = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(models.Project.id.in_(ids))
        )
    }
    teams: dict[UUID, set[UUID]] = {}
    members = await session.execute(
        select(models.ProjectMember.project_id, models.ProjectMember.user_id).where(
            models.ProjectMember.project_id.in_(ids),
            models.ProjectMember.state == MemberState.CONFIRMED,
            models.ProjectMember.left_at.is_(None),
            models.ProjectMember.is_team_row.is_(True),
        )
    )
    for project_id, user_id in members:
        teams.setdefault(project_id, set()).add(user_id)
    event_ids = {project.event_id for project in projects.values() if project.event_id}
    winners = list(
        await session.scalars(
            select(models.EventSourcePerson).where(
                models.EventSourcePerson.event_id.in_(event_ids),
                models.EventSourcePerson.role == SourcePersonRole.WINNER.value,
            )
        )
    )
    page: list[schemas.ResultToVerify] = []
    for result in items:
        project = projects.get(result.project_id)
        if project is None:
            continue
        team = teams.get(project.id, set())
        candidates = sorted(
            (
                row
                for row in winners
                if row.event_id == project.event_id and row.matched_user_id in team
            ),
            key=lambda row: (row.place is None, row.place or 0, row.display_name, str(row.id)),
        )
        base = await build_result(session, result, project, actor, now, admin=True)
        page.append(
            schemas.ResultToVerify(
                **base.model_dump(),
                project=project_ref(project),
                winner_candidates=[_winner_candidate(row) for row in candidates],
            )
        )
    return schemas.ResultToVerifyPage(items=page, next_cursor=next_cursor)


def _verification_snapshot(result: models.ProjectResult) -> dict[str, JsonValue]:
    return {
        "place_status": result.place_status,
        "place": result.place,
        "place_verified_by": str(result.place_verified_by) if result.place_verified_by else None,
        "place_verified_at": result.place_verified_at.isoformat()
        if result.place_verified_at
        else None,
    }


async def verify_result(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.VerifyResultBody,
    actor: models.User,
    now: datetime,
) -> ProjectResultView:
    result = await session.scalar(
        select(models.ProjectResult)
        .where(models.ProjectResult.project_id == project_id)
        .with_for_update()
    )
    if result is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await load_project(session, project_id)
    if result.place_status is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Result claims no place or award")
    target = PlaceStatus.VERIFIED.value if body.decision == "verify" else PlaceStatus.REJECTED.value
    if result.place_status == target:
        return await build_result(session, result, project, actor, now, admin=True)
    before = _verification_snapshot(result)
    result.place_status = target
    result.place_verified_by = actor.id
    result.place_verified_at = now
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.RESULT_VERIFY
        if target == PlaceStatus.VERIFIED.value
        else AuditAction.RESULT_REJECT,
        entity_type=AuditEntityType.PROJECT_RESULT,
        entity_id=project_id,
        before=before,
        after=_verification_snapshot(result),
        reason=body.note,
    )
    for user_id in await team_member_ids(session, project_id):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    await session.flush()
    return await build_result(session, result, project, actor, now, admin=True)
