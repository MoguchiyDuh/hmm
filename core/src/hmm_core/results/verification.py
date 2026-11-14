from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    MemberState,
    PlaceStatus,
    SourcePersonRole,
)
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.projects.service import project_ref
from hmm_core.results import schemas
from hmm_core.results.schemas import ProjectResult as ProjectResultView
from hmm_core.results.submissions import build_result, load_project, team_member_ids


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
        reason=body.note or body.reason,
    )
    for user_id in await team_member_ids(session, project_id):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    await session.flush()
    return await build_result(session, result, project, actor, now, admin=True)
