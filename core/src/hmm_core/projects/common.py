from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models


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
