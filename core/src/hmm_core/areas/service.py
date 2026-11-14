from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ErrorCode
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.areas.schemas import Area, AreaAction, CreateAreaBody, UpdateAreaBody
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job


async def list_areas(session: AsyncSession, *, include_archived: bool) -> list[models.Area]:
    statement = select(models.Area).order_by(models.Area.sort, models.Area.key)
    if not include_archived:
        statement = statement.where(models.Area.archived_at.is_(None))
    return list(await session.scalars(statement))


async def get_area(
    session: AsyncSession, area_id: UUID, *, for_update: bool = False
) -> models.Area:
    statement = select(models.Area).where(models.Area.id == area_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    area = await session.scalar(statement)
    if area is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return area


async def create_area(
    session: AsyncSession, jobs: procrastinate.App, body: CreateAreaBody, actor: UUID
) -> models.Area:
    values = body.model_dump(exclude={"reason"})
    area = await session.scalar(
        insert(models.Area)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[models.Area.key])
        .returning(models.Area)
    )
    if area is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "An area with this key already exists")
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.AREA_CREATE,
        entity_type=AuditEntityType.AREA,
        entity_id=area.id,
        after=values,
        reason=body.reason,
    )
    await defer_job(session, jobs, JobName.RECOMPUTE_ALL_MEMBERS)
    return area


async def update_area(
    session: AsyncSession, area_id: UUID, body: UpdateAreaBody, actor: UUID
) -> models.Area:
    area = await get_area(session, area_id, for_update=True)
    changed: dict[str, JsonValue] = {
        key: value
        for key, value in body.model_dump(exclude={"reason"}, exclude_none=True).items()
        if getattr(area, key) != value
    }
    if not changed:
        return area
    before = {key: getattr(area, key) for key in changed}
    for key, value in changed.items():
        setattr(area, key, value)
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.AREA_EDIT,
        entity_type=AuditEntityType.AREA,
        entity_id=area.id,
        before=before,
        after=changed,
        reason=body.reason,
    )
    return area


async def archive_area(
    session: AsyncSession, area_id: UUID, actor: UUID, reason: str | None, now: datetime
) -> models.Area:
    area = await get_area(session, area_id, for_update=True)
    if area.archived_at is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Area is already archived")
    area.archived_at = now
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.AREA_ARCHIVE,
        entity_type=AuditEntityType.AREA,
        entity_id=area.id,
        before={"archived_at": None},
        after={"archived_at": now.isoformat()},
        reason=reason,
    )
    return area


async def unarchive_area(
    session: AsyncSession, area_id: UUID, actor: UUID, reason: str | None, now: datetime
) -> models.Area:
    area = await get_area(session, area_id, for_update=True)
    if area.archived_at is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Area is not archived")
    before: dict[str, JsonValue] = {"archived_at": area.archived_at.isoformat()}
    area.archived_at = None
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.AREA_UNARCHIVE,
        entity_type=AuditEntityType.AREA,
        entity_id=area.id,
        before=before,
        after={"archived_at": None},
        reason=reason,
    )
    return area


def area_view(area: models.Area, actor: models.User) -> Area:
    actions: list[AreaAction] = []
    if actor.is_admin:
        actions = [
            AreaAction.EDIT,
            AreaAction.ARCHIVE if area.archived_at is None else AreaAction.UNARCHIVE,
        ]
    return Area(
        id=area.id,
        key=area.key,
        name=area.name,
        description=area.description,
        sort=area.sort,
        archived_at=area.archived_at,
        allowed_actions=actions,
    )
