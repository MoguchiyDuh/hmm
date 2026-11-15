from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ActorKind, LeftReason
from hmm.platform.db import locked
from hmm.projects import succession


async def remove_user_from_standalone_projects(
    session: AsyncSession,
    user_id: UUID,
    actor: UUID,
    actor_kind: ActorKind,
    now: datetime,
) -> None:

    user = await session.get(models.User, user_id)
    if user is None:
        return None
    project_ids = sorted(
        set(
            await session.scalars(
                select(models.Project.id)
                .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
                .where(
                    models.ProjectMember.user_id == user_id,
                    models.ProjectMember.left_at.is_(None),
                    models.Project.team_status.is_(None),
                )
            )
        ),
        key=str,
    )
    for project_id in project_ids:
        project = await locked(session, models.Project, project_id)
        if project is None:
            continue
        member = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if member is None:
            continue
        await succession.depart_member(
            session,
            project,
            member,
            user,
            LeftReason.BANNED,
            now,
            actor=actor,
            actor_kind=actor_kind,
        )
