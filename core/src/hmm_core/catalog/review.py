from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    EventStatus,
    Trust,
)
from pydantic import JsonValue
from sqlalchemy import delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.catalog import schemas
from hmm_core.catalog import service as catalog
from hmm_core.formation import service as formation
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job

ATTENDANCE_RANK = {
    AttendanceStatus.CANCELLED.value: 1,
    AttendanceStatus.GOING.value: 2,
    AttendanceStatus.ATTENDED.value: 3,
}
TRUST_RANK = {Trust.SELF.value: 0, Trust.PEER.value: 1, Trust.TRUSTED.value: 2}


async def review_queue(
    session: AsyncSession, actor: models.User, now: datetime
) -> list[schemas.EventDetail]:
    rows = await session.scalars(
        select(models.Event)
        .where(func.cardinality(models.Event.review_reasons) > 0)
        .order_by(models.Event.starts_at, models.Event.id)
    )
    return [await catalog.event_detail(session, row, actor, now) for row in list(rows)]


async def publish_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    actor: models.User,
    reason: str | None,
    now: datetime,
) -> models.Event:
    event = await catalog.load_event(session, event_id, for_update=True)
    if event.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Only drafts and published events can be published"
        )
    previous: dict[str, JsonValue] = {
        "status": event.status,
        "review_reasons": list[JsonValue](event.review_reasons),
    }
    if event.status == EventStatus.PUBLISHED and not event.review_reasons:
        return event
    event.status = EventStatus.PUBLISHED.value
    event.review_reasons = []
    await formation.ensure_formation(session, event, now)
    await catalog.enqueue_lead_fits(session, jobs, event)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_PUBLISH,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before=previous,
        after={"status": "published", "review_reasons": []},
        reason=reason,
    )
    await session.flush()
    await session.refresh(event)
    return event


async def reject_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    actor: models.User,
    reason: str | None,
    now: datetime,
) -> models.Event:
    event = await catalog.load_event(session, event_id, for_update=True)
    if event.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Only drafts and published events can be rejected"
        )
    previous = event.status
    reasons: list[JsonValue] = list(event.review_reasons)
    event.status = EventStatus.ARCHIVED.value
    event.review_reasons = []
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_REJECT,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={"status": previous, "review_reasons": reasons},
        after={"status": "archived", "review_reasons": []},
        reason=reason,
    )
    await session.flush()
    await session.refresh(event)
    return event


async def merge_attendances(
    session: AsyncSession, source: models.Event, target: models.Event, now: datetime
) -> set[UUID]:
    source_rows = list(
        await session.scalars(
            select(models.EventAttendance)
            .where(models.EventAttendance.event_id == source.id)
            .with_for_update()
        )
    )
    target_rows = {
        row.user_id: row
        for row in await session.scalars(
            select(models.EventAttendance)
            .where(models.EventAttendance.event_id == target.id)
            .with_for_update()
        )
    }
    affected: set[UUID] = set()
    for source_row in source_rows:
        affected.add(source_row.user_id)
        merged = target_rows.get(source_row.user_id)
        if merged is None:
            source_row.event_id = target.id
            continue
        status = max(merged.status, source_row.status, key=ATTENDANCE_RANK.__getitem__)
        if status != merged.status:
            merged.status = status
            merged.status_at = now
        target_links = merged.proof_links if isinstance(merged.proof_links, list) else []
        source_links = source_row.proof_links if isinstance(source_row.proof_links, list) else []
        merged.proof_links = list(dict.fromkeys([*target_links, *source_links]))[:10]
        await session.execute(
            insert(models.Confirmation)
            .from_select(
                ["fact_type", "fact_id", "subject_user_id", "confirmer_id", "source", "ignored"],
                select(
                    models.Confirmation.fact_type,
                    literal(merged.id),
                    models.Confirmation.subject_user_id,
                    models.Confirmation.confirmer_id,
                    models.Confirmation.source,
                    models.Confirmation.ignored,
                ).where(
                    models.Confirmation.fact_type == ConfirmationFactType.ATTENDANCE.value,
                    models.Confirmation.fact_id == source_row.id,
                    models.Confirmation.withdrawn_at.is_(None),
                ),
            )
            .on_conflict_do_nothing(
                index_elements=["fact_type", "fact_id", "confirmer_id"],
                index_where=text("withdrawn_at IS NULL"),
            )
        )
        await session.execute(
            delete(models.Confirmation).where(
                models.Confirmation.fact_type == ConfirmationFactType.ATTENDANCE.value,
                models.Confirmation.fact_id == source_row.id,
            )
        )
        merged.trust = max([merged.trust, source_row.trust], key=TRUST_RANK.__getitem__)
        if await session.scalar(
            select(func.count())
            .select_from(models.Confirmation)
            .where(
                models.Confirmation.fact_type == ConfirmationFactType.ATTENDANCE.value,
                models.Confirmation.fact_id == merged.id,
                models.Confirmation.withdrawn_at.is_(None),
                models.Confirmation.source.in_(
                    [ConfirmationSource.SYSTEM.value, ConfirmationSource.ADMIN.value]
                ),
            )
        ):
            merged.trust = Trust.TRUSTED.value
        await session.delete(source_row)
    return affected


async def merge_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.MergeEventBody,
    actor: models.User,
    now: datetime,
) -> models.Event:
    if event_id == body.into:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Cannot merge an event into itself")
    rows = {
        row.id: row
        for row in await session.scalars(
            select(models.Event)
            .where(models.Event.id.in_([event_id, body.into]))
            .order_by(models.Event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    if event_id not in rows or body.into not in rows:
        raise ProblemError(ErrorCode.NOT_FOUND)
    source, target = rows[event_id], rows[body.into]
    previous_status = source.status
    previous_reasons: list[JsonValue] = list(source.review_reasons)
    if source.status == EventStatus.ARCHIVED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Archived events cannot be merged")
    if source.status == EventStatus.CANCELLED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Cancelled events cannot be merged")
    if target.status == EventStatus.ARCHIVED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Target event is archived")
    if target.status == EventStatus.CANCELLED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Target event is cancelled")
    if (
        await session.scalar(
            select(models.Project.id)
            .where(models.Project.event_id == source.id, models.Project.team_status.is_not(None))
            .limit(1)
        )
        is not None
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "Event has teams")
    if (
        await session.scalar(
            select(models.Enrollment.id).where(models.Enrollment.event_id == source.id).limit(1)
        )
        is not None
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "Event has enrollments")
    await session.execute(
        update(models.EventSource)
        .where(models.EventSource.event_id == source.id)
        .values(event_id=target.id)
    )
    await session.execute(
        update(models.EventSourcePerson)
        .where(models.EventSourcePerson.event_id == source.id)
        .values(event_id=target.id)
    )
    affected = await merge_attendances(session, source, target, now)
    source.status = EventStatus.ARCHIVED.value
    source.review_reasons = []
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_MERGE,
        entity_type=AuditEntityType.EVENT,
        entity_id=source.id,
        before={"status": previous_status, "review_reasons": previous_reasons},
        after={"status": "archived", "merged_into": str(target.id)},
        reason=body.reason,
    )
    for user_id in sorted(affected, key=str):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    await session.flush()
    await session.refresh(target)
    return target
