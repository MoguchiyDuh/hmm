from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from hashlib import sha1
from typing import Any
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    EnrollmentStatus,
    ErrorCode,
    EventRevisionOrigin,
    EventStatus,
    FormationPhase,
    InviteStatus,
    JoinRequestStatus,
    LeadOfferStatus,
    MergeProposalStatus,
    ProjectStatus,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_contracts.notifications import (
    AlertKind,
    EventCancelledPayload,
    EventChangedPayload,
    NotificationChange,
)
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.catalog import schemas
from hmm_core.catalog.events import (
    LIVE_ENROLLMENTS,
    LOCKABLE_SKIP,
    TEAM_TERMINAL,
    EventAreaInput,
    enqueue_lead_fits,
    load_event,
    resolve_areas,
)
from hmm_core.formation import service as formation
from hmm_core.notifications.service import notify, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError


async def event_recipients(session: AsyncSession, event_id: UUID) -> list[UUID]:
    recipients = await session.scalars(
        select(models.EventAttendance.user_id)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.status == AttendanceStatus.GOING,
        )
        .union(
            select(models.Enrollment.user_id).where(
                models.Enrollment.event_id == event_id,
                models.Enrollment.status.in_(LIVE_ENROLLMENTS),
            ),
            select(models.ProjectMember.user_id).where(
                models.ProjectMember.event_id == event_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row,
            ),
        )
    )
    return sorted(recipients, key=str)


async def notify_event_changed(
    session: AsyncSession,
    event: models.Event,
    changes: Sequence[NotificationChange],
    now: datetime,
) -> None:
    if not changes:
        return
    recipients = await event_recipients(session, event.id)
    if not recipients:
        return
    fields_hash = sha1("|".join(sorted(change.field for change in changes)).encode()).hexdigest()[
        :12
    ]
    base = f"event_changed:{event.id}:{int(now.timestamp())}:{fields_hash}"
    payload = EventChangedPayload(
        link=f"/events/{event.slug}",
        event_id=event.id,
        event_title=event.title,
        changes=tuple(changes),
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        timezone=event.timezone,
    )
    for user_id in recipients:
        await notify(session, user_id, payload, now, dedupe_key=f"{base}:{user_id}")


async def notify_event_cancelled(
    session: AsyncSession,
    event: models.Event,
    recipients: Sequence[UUID],
    reason: str,
    now: datetime,
) -> None:
    if not recipients:
        return
    payload = EventCancelledPayload(
        link=f"/events/{event.slug}",
        event_id=event.id,
        event_title=event.title,
        reason=reason,
    )
    for user_id in recipients:
        await notify(
            session,
            user_id,
            payload,
            now,
            dedupe_key=f"event_cancelled:{event.id}:{user_id}",
        )


async def cancel_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    *,
    reason: str,
    actor: UUID | None,
    actor_kind: ActorKind,
    now: datetime,
) -> models.Event:
    event = await load_event(session, event_id, for_update=True)
    if event.status == EventStatus.ARCHIVED:
        raise ProblemError(ErrorCode.INVALID_STATE, "Archived events cannot be cancelled")
    if event.status == EventStatus.CANCELLED:
        return event
    previous_status = event.status
    recipients = await event_recipients(session, event_id)
    cancellable_teams = [
        TeamStatus.FORMING,
        TeamStatus.REVIEW,
        TeamStatus.LOCKED,
        TeamStatus.ACTIVE,
    ]
    team_ids = list(
        await session.scalars(
            select(models.Project.id).where(
                models.Project.event_id == event_id,
                models.Project.team_status.in_(cancellable_teams),
            )
        )
    )
    event.status = EventStatus.CANCELLED.value
    await session.execute(
        update(models.EventFormation)
        .where(models.EventFormation.event_id == event_id)
        .values(phase=FormationPhase.CLOSED.value)
    )
    await session.execute(
        update(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.status == AttendanceStatus.GOING,
        )
        .values(status=AttendanceStatus.CANCELLED.value, status_at=now)
    )
    await session.execute(
        update(models.Project)
        .where(
            models.Project.event_id == event_id,
            models.Project.team_status.in_(cancellable_teams),
        )
        .values(team_status=TeamStatus.CANCELLED.value, status=ProjectStatus.CANCELLED.value)
    )
    await session.execute(
        update(models.Enrollment)
        .where(
            models.Enrollment.event_id == event_id,
            models.Enrollment.status.in_(LIVE_ENROLLMENTS),
        )
        .values(status=EnrollmentStatus.EXPIRED.value, status_at=now)
    )
    await session.execute(
        update(models.Invite)
        .where(models.Invite.event_id == event_id, models.Invite.status == InviteStatus.PENDING)
        .values(status=InviteStatus.CLOSED.value, closed_reason="event_cancelled")
    )
    for model, pending, closed in (
        (models.JoinRequest, JoinRequestStatus.PENDING.value, JoinRequestStatus.CLOSED.value),
        (models.SwapRequest, SwapRequestStatus.PENDING.value, SwapRequestStatus.CLOSED.value),
        (models.MergeProposal, MergeProposalStatus.PENDING.value, MergeProposalStatus.CLOSED.value),
        (models.LeadOffer, LeadOfferStatus.PENDING.value, LeadOfferStatus.CLOSED.value),
    ):
        await session.execute(
            update(model)
            .where(model.event_id == event_id, model.status == pending)
            .values(status=closed)
        )
    await notify_event_cancelled(session, event, recipients, reason, now)
    if team_ids:
        await raise_alert(
            session,
            AlertKind.EVENT_CANCELLED_WITH_TEAMS,
            now,
            title=f"Event cancelled with teams: {event.title}",
            message=f"{len(team_ids)} teams were cancelled with the event",
            link=f"/admin/events/{event.id}",
            subject_type="event",
            subject_id=event.id,
            subject_title=event.title,
            dedupe_key=f"event_cancelled_with_teams:{event.id}",
        )
    await record_audit(
        session,
        actor=actor,
        actor_kind=actor_kind,
        action=AuditAction.EVENT_CANCEL,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={"status": previous_status},
        after={"status": EventStatus.CANCELLED.value},
        reason=reason,
    )
    await session.refresh(event)
    return event


def _json(value: Any) -> JsonValue:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal | UUID | Enum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return value


async def update_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.UpdateEventBody,
    actor: models.User,
    now: datetime,
) -> models.Event:
    event = await load_event(session, event_id, for_update=True)
    if body.status is not None and body.status != event.status:
        if body.status == EventStatus.CANCELLED:
            return await cancel_event(
                session,
                jobs,
                event_id,
                reason=body.reason or "Cancelled by admin",
                actor=actor.id,
                actor_kind=ActorKind.ADMIN,
                now=now,
            )
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Publishing and archiving use their own endpoints"
        )
    raw = body.model_dump(exclude_unset=True)
    raw.pop("reason", None)
    raw.pop("status", None)
    area_input = raw.pop("areas", None)
    for field in ("location", "prize"):
        if field in raw and (value := getattr(body, field)) is not None:
            raw[field] = value.model_dump(mode="json", exclude_none=True) or None
    if raw.get("difficulty") is not None:
        raw["difficulty"] = Decimal(str(raw["difficulty"])).quantize(Decimal("0.1"), ROUND_HALF_UP)
    if "slug" in raw and await session.scalar(
        select(exists().where(models.Event.slug == raw["slug"], models.Event.id != event_id))
    ):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Event slug already exists")
    for field in ("website_url", "registration_url"):
        if field in raw and raw[field] is not None:
            raw[field] = str(raw[field])
    for field in (
        "slug",
        "kind",
        "formation_enabled",
        "title",
        "starts_at",
        "ends_at",
        "timezone",
        "language",
        "themes",
    ):
        if field in raw and raw[field] is None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"{field} cannot be null")
    merged = {
        field: raw.get(field, getattr(event, field))
        for field in ("starts_at", "ends_at", "team_size_min", "team_size_max")
    }
    if merged["ends_at"] < merged["starts_at"]:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "ends_at must not precede starts_at")
    min_size, max_size = merged["team_size_min"], merged["team_size_max"]
    if min_size is not None and max_size is not None and min_size > max_size:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR, "team_size_min must not exceed team_size_max"
        )
    if any(size is not None and not 1 <= size <= 20 for size in (min_size, max_size)):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "team sizes must be between 1 and 20")
    if body.formation_enabled is True and not event.formation_enabled:
        if max_size is not None and max_size < 2:
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Formation needs team_size_max of at least 2"
            )
        if await session.scalar(
            select(
                exists().where(
                    models.Enrollment.event_id == event_id,
                    models.Enrollment.status.in_(LIVE_ENROLLMENTS),
                )
                | exists().where(
                    models.Project.event_id == event_id,
                    models.Project.team_status.is_not(None),
                    models.Project.team_status.not_in(TEAM_TERMINAL),
                )
            )
        ):
            raise ProblemError(
                ErrorCode.INVALID_STATE, "Formation can only be enabled before enrollments"
            )
    changes = {
        field: value for field, value in raw.items() if _json(value) != _json(getattr(event, field))
    }
    before_values = {field: getattr(event, field) for field in changes}
    resolved_areas: list[tuple[models.Area, EventAreaInput]] | None = None
    if area_input is not None:
        resolved_areas = await resolve_areas(
            session,
            [
                EventAreaInput(key=area.key, importance=area.importance, headcount=area.headcount)
                for area in body.areas or ()
            ],
        )
        old_areas = [
            {"key": key, "importance": importance, "headcount": headcount}
            for key, importance, headcount in await session.execute(
                select(models.Area.key, models.EventArea.importance, models.EventArea.headcount)
                .join(models.EventArea, models.EventArea.area_id == models.Area.id)
                .where(models.EventArea.event_id == event_id)
                .order_by(models.Area.key)
            )
        ]
        new_areas = [
            {
                "key": area.key,
                "importance": area_input.importance.value,
                "headcount": area_input.headcount,
            }
            for area, area_input in sorted(resolved_areas, key=lambda item: item[0].key)
        ]
        if old_areas != new_areas:
            changes["areas"] = new_areas
            before_values["areas"] = old_areas
    if not changes:
        return event
    previous_dates = formation.event_dates(event)
    for field, value in changes.items():
        if field != "areas":
            setattr(event, field, value.value if isinstance(value, Enum) else value)
    if "areas" in changes and resolved_areas is not None:
        await session.execute(delete(models.EventArea).where(models.EventArea.event_id == event_id))
        session.add_all(
            models.EventArea(
                event_id=event_id,
                area_id=area.id,
                importance=area_input.importance.value,
                headcount=area_input.headcount,
            )
            for area, area_input in resolved_areas
        )
    for field in sorted(changes.keys() - LOCKABLE_SKIP):
        await session.execute(
            insert(models.EventFieldLock)
            .values(event_id=event_id, field=field, locked_by=actor.id, locked_at=now)
            .on_conflict_do_nothing(index_elements=["event_id", "field"])
        )
    before = {field: _json(value) for field, value in before_values.items()}
    after = {field: _json(value) for field, value in changes.items()}
    diff: dict[str, JsonValue] = {
        field: {"before": before[field], "after": after[field]} for field in changes
    }
    session.add(
        models.EventRevision(
            event_id=event_id,
            origin=EventRevisionOrigin.ADMIN.value,
            actor_id=actor.id,
            diff=diff,
            ignored_fields=[],
        )
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_EDIT,
        entity_type=AuditEntityType.EVENT,
        entity_id=event_id,
        before=before,
        after=after,
        reason=body.reason,
    )
    if "formation_enabled" in changes and event.formation_enabled:
        await formation.ensure_formation(session, event, now)
    if changes.keys() & {"formation_enabled", "areas"}:
        await enqueue_lead_fits(session, jobs, event)
    notification_fields = changes.keys() & {
        "starts_at",
        "ends_at",
        "team_size_min",
        "team_size_max",
        "format",
        "registration_deadline",
    }
    if notification_fields:
        await notify_event_changed(
            session,
            event,
            [
                NotificationChange(
                    field=field,
                    previous=str(_json(before_values[field]))
                    if before_values[field] is not None
                    else None,
                    current=str(_json(getattr(event, field)))
                    if getattr(event, field) is not None
                    else None,
                )
                for field in sorted(notification_fields)
            ],
            now,
        )
    if changes.keys() & {"starts_at", "ends_at", "registration_deadline"}:
        await formation.dates_changed(session, jobs, event, previous_dates, now)
    from hmm_core.recommendations import service as recommendations

    await recommendations.enqueue_event_refresh(session, jobs, event_id)
    await session.flush()
    await session.refresh(event)
    return event


async def list_locks(session: AsyncSession, event_id: UUID) -> list[schemas.EventFieldLock]:
    await load_event(session, event_id)
    return [
        schemas.EventFieldLock(
            event_id=lock.event_id,
            field=lock.field,
            locked_by=lock.locked_by,
            locked_at=lock.locked_at,
            allowed_actions=[schemas.EventFieldLockAction.UNLOCK],
        )
        for lock in await session.scalars(
            select(models.EventFieldLock)
            .where(models.EventFieldLock.event_id == event_id)
            .order_by(
                models.EventFieldLock.field,
                models.EventFieldLock.locked_by,
                models.EventFieldLock.locked_at,
            )
        )
    ]


async def unlock_field(
    session: AsyncSession,
    event_id: UUID,
    field: str,
    actor: models.User,
    reason: str | None,
    now: datetime,
) -> None:
    event = await load_event(session, event_id)
    lock = await session.scalar(
        select(models.EventFieldLock)
        .where(models.EventFieldLock.event_id == event_id, models.EventFieldLock.field == field)
        .with_for_update()
    )
    if lock is None:
        raise ProblemError(ErrorCode.NOT_FOUND, "Field is not locked")
    await session.delete(lock)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.EVENT_UNLOCK_FIELD,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={
            "field": field,
            "locked_by": str(lock.locked_by),
            "locked_at": lock.locked_at.isoformat(),
        },
        after=None,
        reason=reason,
    )


async def list_revisions(session: AsyncSession, event_id: UUID) -> list[schemas.EventRevision]:
    await load_event(session, event_id)
    return [
        schemas.EventRevision(
            id=revision.id,
            event_id=revision.event_id,
            origin=EventRevisionOrigin(revision.origin),
            actor_id=revision.actor_id,
            diff=TypeAdapter(dict[str, JsonValue]).validate_python(revision.diff),
            ignored_fields=list(revision.ignored_fields),
            created_at=revision.created_at,
        )
        for revision in await session.scalars(
            select(models.EventRevision)
            .where(models.EventRevision.event_id == event_id)
            .order_by(models.EventRevision.created_at.desc(), models.EventRevision.id.desc())
        )
    ]
