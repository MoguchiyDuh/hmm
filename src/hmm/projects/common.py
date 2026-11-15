from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ErrorCode, ProjectVisibility
from hmm.platform.errors import ProblemError
from hmm.projects.views import project_closed


async def project_member_ids(session: AsyncSession, project_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def is_project_member(
    session: AsyncSession, project: models.Project, actor: models.User
) -> bool:
    return (
        await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == actor.id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        is not None
    )


async def visible_project(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> models.Project:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if (
        project.visibility == ProjectVisibility.MEMBERS.value
        and not actor.is_admin
        and actor.id != project.lead_user_id
        and not await is_project_member(session, project, actor)
    ):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


async def member_project(
    session: AsyncSession, project_id: UUID, actor: models.User, *, lock: bool = False
) -> models.Project:
    if lock:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        if (
            project.visibility == ProjectVisibility.MEMBERS.value
            and not actor.is_admin
            and actor.id != project.lead_user_id
            and not await is_project_member(session, project, actor)
        ):
            raise ProblemError(ErrorCode.NOT_FOUND)
    else:
        project = await visible_project(session, project_id, actor)
    if not await is_project_member(session, project, actor):
        raise ProblemError(ErrorCode.FORBIDDEN)
    return project


async def writable_member_project(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> models.Project:
    project = await member_project(session, project_id, actor, lock=True)
    if project_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    return project
