from datetime import datetime
from uuid import UUID

import procrastinate
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.projects import schemas
from hmm_core.projects.common import visible_project, writable_member_project
from hmm_core.projects.idle import mark_project_progress


def update_view(update: models.ProjectUpdate) -> schemas.ProjectUpdate:
    return schemas.ProjectUpdate.model_validate(
        {
            "id": update.id,
            "project_id": update.project_id,
            "author_id": update.author_id,
            "body_md": update.body_md,
            "links": [HttpUrl(str(link)) for link in update.links]
            if isinstance(update.links, list)
            else [],
            "created_at": update.created_at,
            "deleted_at": update.deleted_at,
        }
    )


async def list_updates(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[schemas.ProjectUpdate]:
    project = await visible_project(session, project_id, actor)
    rows = await session.scalars(
        select(models.ProjectUpdate)
        .where(
            models.ProjectUpdate.project_id == project.id,
            models.ProjectUpdate.deleted_at.is_(None),
        )
        .order_by(models.ProjectUpdate.created_at.desc(), models.ProjectUpdate.id.desc())
    )
    return [update_view(row) for row in rows]


async def create_update(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.CreateProjectUpdateBody,
    actor: models.User,
    now: datetime,
) -> schemas.ProjectUpdate:
    project = await writable_member_project(session, project_id, actor)
    mark_project_progress(project, now)
    update = models.ProjectUpdate(
        project_id=project.id,
        author_id=actor.id,
        body_md=body.body_md,
        links=[str(link) for link in body.links],
        deleted_at=None,
    )
    session.add(update)
    await session.flush()
    await session.refresh(update)
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(actor.id))
    return update_view(update)
