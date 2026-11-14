from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import ErrorCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.projects import schemas
from hmm_core.projects.common import (
    is_project_member,
    visible_project,
    writable_member_project,
)
from hmm_core.projects.idle import mark_project_progress


def milestone_view(milestone: models.ProjectMilestone, *, is_member: bool) -> schemas.Milestone:
    return schemas.Milestone.model_validate(
        {
            "id": milestone.id,
            "project_id": milestone.project_id,
            "title": milestone.title,
            "description": milestone.description,
            "due_on": milestone.due_on,
            "position": milestone.position,
            "done_at": milestone.done_at,
            "done_by": milestone.done_by,
            "created_at": milestone.created_at,
            "updated_at": milestone.updated_at,
            "allowed_actions": [action for action in schemas.MilestoneAction if is_member],
        }
    )


async def list_milestones(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[schemas.Milestone]:
    project = await visible_project(session, project_id, actor)
    member = await is_project_member(session, project, actor)
    rows = await session.scalars(
        select(models.ProjectMilestone)
        .where(models.ProjectMilestone.project_id == project.id)
        .order_by(models.ProjectMilestone.position, models.ProjectMilestone.id)
    )
    return [milestone_view(row, is_member=member) for row in rows]


async def create_milestone(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateMilestoneBody,
    actor: models.User,
    now: datetime,
) -> schemas.Milestone:
    project = await writable_member_project(session, project_id, actor)
    mark_project_progress(project, now, start_idea=False)
    await session.flush()
    milestone = models.ProjectMilestone(
        project_id=project.id,
        title=body.title,
        description=body.description,
        due_on=body.due_on,
        position=body.position,
        done_at=None,
        done_by=None,
    )
    session.add(milestone)
    await session.flush()
    await session.refresh(milestone)
    return milestone_view(milestone, is_member=True)


async def update_milestone(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    milestone_id: UUID,
    body: schemas.UpdateMilestoneBody,
    actor: models.User,
    now: datetime,
) -> schemas.Milestone:
    project = await writable_member_project(session, project_id, actor)
    milestone = await session.scalar(
        select(models.ProjectMilestone)
        .where(
            models.ProjectMilestone.id == milestone_id,
            models.ProjectMilestone.project_id == project.id,
        )
        .with_for_update()
    )
    if milestone is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    changed = False
    for field in ("title", "description", "position", "due_on"):
        value = getattr(body, field)
        if value is not None and value != getattr(milestone, field):
            setattr(milestone, field, value)
            changed = True
    previous_done_by = milestone.done_by
    newly_done = False
    if body.done is not None and body.done != (milestone.done_at is not None):
        milestone.done_at = now if body.done else None
        milestone.done_by = actor.id if body.done else None
        newly_done = body.done
        changed = True
    if not changed:
        return milestone_view(milestone, is_member=True)
    mark_project_progress(project, now, start_idea=newly_done)
    await session.flush()
    if previous_done_by != milestone.done_by:
        affected = {
            user_id for user_id in (previous_done_by, milestone.done_by) if user_id is not None
        }
        for user_id in sorted(affected, key=str):
            await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    await session.refresh(milestone)
    return milestone_view(milestone, is_member=True)


async def delete_milestone(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    milestone_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    project = await writable_member_project(session, project_id, actor)
    milestone = await session.scalar(
        select(models.ProjectMilestone)
        .where(
            models.ProjectMilestone.id == milestone_id,
            models.ProjectMilestone.project_id == project.id,
        )
        .with_for_update()
    )
    if milestone is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    done_by = milestone.done_by
    await session.delete(milestone)
    mark_project_progress(project, now, start_idea=False)
    await session.flush()
    if done_by is not None:
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(done_by))
