from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import ErrorCode, ProjectStatus, ProjectVisibility, TeamStatus
from hmm_contracts.notifications import ProjectPausedPayload
from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify_many
from hmm_core.platform.errors import ProblemError
from hmm_core.projects import schemas
from hmm_core.projects.common import project_member_ids
from hmm_core.projects.views import CLOSED_PROJECT_STATUSES, lifecycle_open, project_response


async def _locked_project(session: AsyncSession, project_id: UUID) -> models.Project:
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


async def _transition(
    session: AsyncSession,
    project_id: UUID,
    actor: models.User,
    now: datetime,
    *,
    target: ProjectStatus,
    allowed_from: Sequence[ProjectStatus],
    message: str,
    notify_members: bool,
) -> schemas.Project:
    project = await _locked_project(session, project_id)
    if not lifecycle_open(project):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "This project's lifecycle follows its hackathon team"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.status not in {status.value for status in allowed_from}:
        raise ProblemError(ErrorCode.INVALID_STATE, message)
    project.status = target.value
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    if notify_members:
        members = [uid for uid in await project_member_ids(session, project.id) if uid != actor.id]
        await notify_many(
            session,
            members,
            ProjectPausedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                status=ProjectStatus.PAUSED,
                reason="The lead paused the project",
            ),
            now,
            dedupe_key=None,
        )
    await _sync_recruiting(session, project, actor.id, now)
    await session.flush()
    return await project_response(session, project.id, actor, now)


async def start_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ACTIVE,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.PAUSED),
        message="This project cannot be started",
        notify_members=False,
    )


async def pause_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.PAUSED,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.ACTIVE),
        message="This project cannot be paused",
        notify_members=True,
    )


async def abandon_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ABANDONED,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.ACTIVE, ProjectStatus.PAUSED),
        message="This project cannot be abandoned",
        notify_members=False,
    )


async def continue_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ACTIVE,
        allowed_from=(ProjectStatus.SHIPPED,),
        message="This project cannot be continued",
        notify_members=False,
    )


async def update_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.UpdateProjectBody,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.feed import service as feed
    from hmm_core.recommendations import service as recommendations

    project = await _locked_project(session, project_id)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.team_status in (TeamStatus.DISBANDED.value, TeamStatus.CANCELLED.value):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    changed = False
    became_members = False
    for field in ("name", "description_md", "goal"):
        value = getattr(body, field)
        if value is not None and value != getattr(project, field):
            setattr(project, field, value)
            changed = True
    if body.links is not None:
        links: list[JsonValue] = [str(link) for link in body.links]
        if links != project.links:
            project.links = links
            changed = True
    if body.visibility is not None and body.visibility.value != project.visibility:
        project.visibility = body.visibility.value
        became_members = body.visibility == ProjectVisibility.MEMBERS
        changed = True
    if body.area_keys is not None:
        area_ids = await resolve_project_areas(session, body.area_keys)
        current_ids = list(
            await session.scalars(
                select(models.ProjectArea.area_id).where(
                    models.ProjectArea.project_id == project.id
                )
            )
        )
        if set(area_ids) != set(current_ids):
            await session.execute(
                delete(models.ProjectArea).where(models.ProjectArea.project_id == project.id)
            )
            session.add_all(
                [models.ProjectArea(project_id=project.id, area_id=area_id) for area_id in area_ids]
            )
            changed = True
    if "recruiting" in body.model_fields_set:
        if project.team_status is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "Hackathon teams recruit through formation")
        new_recruiting: JsonValue = None
        if body.recruiting is not None:
            await resolve_project_areas(session, body.recruiting.areas)
            new_recruiting = {
                "areas": [str(key) for key in body.recruiting.areas],
                "note": body.recruiting.note,
            }
        if new_recruiting != project.recruiting:
            project.recruiting = new_recruiting
            changed = True
    if not changed:
        return await project_response(session, project.id, actor, now)
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    if became_members:
        await feed.hide_feed_subject(session, "project", project.id, now)
    await _sync_recruiting(session, project, actor.id, now)
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    return await project_response(session, project.id, actor, now)


async def resolve_project_areas(session: AsyncSession, keys: Sequence[str]) -> list[UUID]:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Duplicate area: {key}")
        seen.add(key)
    if not keys:
        return []
    found: dict[str, UUID] = {
        key: area_id
        for area_id, key in await session.execute(
            select(models.Area.id, models.Area.key).where(
                models.Area.key.in_(list(dict.fromkeys(keys))), models.Area.archived_at.is_(None)
            )
        )
    }
    for key in keys:
        if key not in found:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Unknown area: {key}")
    return [found[key] for key in keys]


async def _sync_recruiting(
    session: AsyncSession, project: models.Project, actor_id: UUID, now: datetime
) -> None:
    from hmm_core.feed import service as feed

    await feed.record_project_recruiting(session, project, actor_id, now)
