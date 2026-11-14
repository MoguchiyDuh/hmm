import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AreaImportance,
    AttendanceRole,
    AttendanceStatus,
    AuditAction,
    AuditEntityType,
    ConfirmationFactType,
    ErrorCode,
    EventFormat,
    EventKind,
    EventOrigin,
    EventReviewReason,
    EventRevisionOrigin,
    EventStatus,
    RestrictionKind,
    Trust,
    ValidationIssue,
)
from hmm_contracts.imports import Location
from hmm_contracts.notifications import NotificationChange
from pydantic import JsonValue
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import schemas
from hmm_core.attendance.service import attendance_view
from hmm_core.catalog import service as catalog
from hmm_core.feed import confirmations, digests, timeline
from hmm_core.formation import service as formation
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
from hmm_core.platform.schemas import EventAction
from hmm_core.platform.settings import load_settings


async def set_attendance(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.SetAttendanceBody,
    actor: models.User,
    now: datetime,
) -> schemas.Attendance:
    settings = await load_settings(session)
    event = await catalog.load_event(session, event_id, for_update=True)
    await catalog.visible_event(session, event_id, actor)
    if body.status == AttendanceStatus.GOING and (
        event.status != "published" or now >= event.ends_at
    ):
        raise ProblemError(ErrorCode.ATTENDANCE_WINDOW_CLOSED, "Going closes at the event end")
    if body.status == AttendanceStatus.ATTENDED and (
        event.status != "published"
        or not (
            event.starts_at <= now <= event.ends_at + timedelta(days=settings.attendance_claim_days)
        )
    ):
        raise ProblemError(
            ErrorCode.ATTENDANCE_WINDOW_CLOSED,
            "Attendance can be claimed from the start until attendance_claim_days after the end",
        )
    attendance = await session.scalar(
        select(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == actor.id,
        )
        .with_for_update()
    )
    if (
        attendance is not None
        and attendance.status == AttendanceStatus.ATTENDED
        and body.status == AttendanceStatus.GOING
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "Attended cannot be changed back to going")
    proof_links: list[JsonValue] = [str(link) for link in body.proof_links]
    previous_status: str | None
    if attendance is None:
        attendance = models.EventAttendance(
            event_id=event_id,
            user_id=actor.id,
            status=body.status.value,
            role=body.role.value,
            proof_links=proof_links,
            note=body.note,
            trust=Trust.SELF.value,
            status_at=now,
        )
        session.add(attendance)
        await session.flush()
        status_changed = True
        previous_status = None
        metadata_changed = False
    else:
        previous_status = attendance.status
        status_changed = previous_status != body.status.value
        metadata_changed = (
            attendance.role != body.role.value
            or attendance.proof_links != proof_links
            or attendance.note != body.note
        )
        if not status_changed and not metadata_changed:
            return attendance_view(attendance, event, actor, actor)
        attendance.status = body.status.value
        attendance.role = body.role.value
        attendance.proof_links = proof_links
        attendance.note = body.note
        if status_changed:
            attendance.status_at = now
        await session.flush()
    if (
        status_changed
        and (
            previous_status == AttendanceStatus.ATTENDED or body.status == AttendanceStatus.ATTENDED
        )
    ) or (not status_changed and metadata_changed):
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(actor.id))
    if body.status == AttendanceStatus.GOING and previous_status != AttendanceStatus.GOING:
        await timeline.record_event_going(session, event.id, actor.id, now)
    if body.status == AttendanceStatus.ATTENDED and now >= event.ends_at:
        await digests.send_attendance_digest(session, event.id, now)
    return attendance_view(attendance, event, actor, actor)


async def delete_attendance(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    await catalog.load_event(session, event_id, for_update=True)
    await catalog.visible_event(session, event_id, actor)
    attendance = await session.scalar(
        select(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == actor.id,
        )
        .with_for_update()
    )
    if attendance is None:
        raise ProblemError(ErrorCode.NOT_FOUND, "No attendance to remove")
    if attendance.status == AttendanceStatus.CANCELLED:
        return None
    previous = attendance.status
    attendance.status = AttendanceStatus.CANCELLED.value
    attendance.status_at = now
    await session.flush()
    if previous == AttendanceStatus.ATTENDED:
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(actor.id))
    return None


async def list_attendees(
    session: AsyncSession,
    event_id: UUID,
    actor: models.User,
    params: CursorParams,
    *,
    status: AttendanceStatus | None,
    now: datetime,
) -> schemas.AttendancePage:
    event = await catalog.visible_event(session, event_id, actor)
    if status == AttendanceStatus.CANCELLED:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "status must be going or attended")
    statuses = (
        [status] if status is not None else [AttendanceStatus.GOING, AttendanceStatus.ATTENDED]
    )
    statement = select(models.EventAttendance).where(
        models.EventAttendance.event_id == event_id,
        models.EventAttendance.status.in_([value.value for value in statuses]),
    )
    rows = list(
        await session.scalars(
            apply_keyset(
                statement,
                params,
                sort=[models.EventAttendance.status_at],
                id_column=models.EventAttendance.id,
            )
        )
    )
    items = rows[: params.limit]
    next_cursor = (
        encode_cursor([items[-1].status_at, items[-1].id]) if len(rows) > params.limit else None
    )
    users = {
        user.id: user
        for user in await session.scalars(
            select(models.User).where(models.User.id.in_([row.user_id for row in items]))
        )
    }
    candidates = await confirmations.confirmation_candidates(session, actor.id, now)
    confirmable = {
        candidate.fact_id
        for candidate in candidates
        if candidate.fact_type == ConfirmationFactType.ATTENDANCE
    }
    return schemas.AttendancePage(
        items=[
            attendance_view(
                row, event, users[row.user_id], actor, can_confirm=row.id in confirmable
            )
            for row in items
        ],
        next_cursor=next_cursor,
    )


async def list_my_attendances(
    session: AsyncSession, actor: models.User, *, status: AttendanceStatus | None
) -> list[schemas.Attendance]:
    statement = select(models.EventAttendance).where(models.EventAttendance.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.EventAttendance.status == status.value)
    rows = list(
        await session.scalars(
            statement.order_by(
                models.EventAttendance.status_at.desc(), models.EventAttendance.id.desc()
            )
        )
    )
    events = {
        event.id: event
        for event in await session.scalars(
            select(models.Event).where(models.Event.id.in_({row.event_id for row in rows}))
        )
    }
    return [attendance_view(row, events[row.event_id], actor, actor) for row in rows]


def normalize_event_title(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", title.casefold())
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    return " ".join(text.split())


async def duplicate_candidates(
    session: AsyncSession,
    *,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    website_url: str | None,
) -> list[models.Event]:
    eligible = models.Event.status.not_in([EventStatus.ARCHIVED.value, EventStatus.CANCELLED.value])
    found: dict[UUID, models.Event] = {}
    if website_url is not None:
        for row in await session.scalars(
            select(models.Event).where(eligible, models.Event.website_url == website_url)
        ):
            found[row.id] = row
    normalized = normalize_event_title(title)
    for row in await session.scalars(
        select(models.Event)
        .where(
            eligible,
            models.Event.starts_at <= ends_at + timedelta(days=1),
            models.Event.ends_at >= starts_at - timedelta(days=1),
        )
        .order_by(models.Event.starts_at, models.Event.id)
        .limit(50)
    ):
        if normalize_event_title(row.title) == normalized:
            found[row.id] = row
    return sorted(found.values(), key=lambda row: (row.starts_at, row.id))[:20]


async def member_event_view(
    session: AsyncSession, event: models.Event, actor: models.User, now: datetime
) -> schemas.MemberEvent:
    area_keys = list(
        await session.scalars(
            select(models.Area.key)
            .join(models.EventArea, models.EventArea.area_id == models.Area.id)
            .where(models.EventArea.event_id == event.id)
            .order_by(models.Area.sort, models.Area.key)
        )
    )
    allowed: set[EventAction] = set()
    if actor.is_admin or (event.created_by == actor.id and now < event.starts_at):
        allowed.add(EventAction.EDIT)
    if actor.is_admin or (
        event.created_by == actor.id and event.status == EventStatus.PUBLISHED.value
    ):
        allowed.add(EventAction.CANCEL)
    return schemas.MemberEvent(
        id=event.id,
        title=event.title,
        kind=EventKind(event.kind),
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        timezone=event.timezone,
        slug=event.slug,
        summary=event.summary,
        description_md=event.description_md,
        format=EventFormat(event.format) if event.format is not None else EventFormat.ONLINE,
        location=Location.model_validate(event.location) if event.location else None,
        website_url=event.website_url,
        organizer=event.organizer,
        registration_url=event.registration_url,
        registration_opens_at=event.registration_opens_at,
        registration_deadline=event.registration_deadline,
        language=list(event.language),
        eligibility=event.eligibility,
        themes=list(event.themes),
        team_size_min=event.team_size_min,
        team_size_max=event.team_size_max,
        area_keys=area_keys,
        status=EventStatus(event.status),
        origin=EventOrigin(event.origin),
        created_by=event.created_by,
        formation_enabled=event.formation_enabled,
        review_reasons=[EventReviewReason(reason) for reason in event.review_reasons],
        created_at=event.created_at,
        updated_at=event.updated_at,
        allowed_actions=[action for action in EventAction if action in allowed],
    )


async def create_member_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    body: schemas.CreateMemberEventBody,
    actor: models.User,
    *,
    confirm_new: bool,
    now: datetime,
) -> models.Event:
    restricted = await session.scalar(
        select(models.MemberRestriction.id)
        .where(
            models.MemberRestriction.user_id == actor.id,
            models.MemberRestriction.kind == RestrictionKind.CREATING_EVENTS.value,
            models.MemberRestriction.lifted_at.is_(None),
            or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
        )
        .limit(1)
    )
    if restricted is not None:
        raise ProblemError(ErrorCode.RESTRICTED, "Creating events is restricted for this member")
    if body.ends_at < body.starts_at:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "ends_at must not precede starts_at")
    website_url = str(body.website_url) if body.website_url is not None else None
    candidates = await duplicate_candidates(
        session,
        title=body.title,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        website_url=website_url,
    )
    if candidates and not confirm_new:
        raise ProblemError(
            ErrorCode.POSSIBLE_DUPLICATE,
            "Similar events exist; resend with confirm_new=true",
            errors=[ValidationIssue(loc=[str(row.id)], msg=row.title) for row in candidates],
        )
    reasons = [EventReviewReason.POSSIBLE_DUPLICATE] if candidates else []
    payload = catalog.EventInput(
        kind=body.kind,
        title=body.title,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        timezone=body.timezone,
        formation_enabled=False,
        format=body.format,
        location=(body.location.model_dump(mode="json", exclude_none=True) or None)
        if body.location is not None
        else None,
        website_url=website_url,
        description_md=body.description_md,
        areas=[
            catalog.EventAreaInput(key=key, importance=AreaImportance.PRIMARY)
            for key in body.area_keys
        ],
    )
    event = await catalog.create_event(
        session,
        payload,
        origin=EventOrigin.MEMBER,
        status=EventStatus.PUBLISHED,
        created_by=actor.id,
        now=now,
        review_reasons=reasons,
    )
    status = AttendanceStatus.ATTENDED if event.ends_at <= now else AttendanceStatus.GOING
    session.add(
        models.EventAttendance(
            event_id=event.id,
            user_id=actor.id,
            status=status.value,
            role=AttendanceRole.ORGANIZER.value,
            proof_links=[],
            note=None,
            trust=Trust.SELF.value,
            status_at=now,
        )
    )
    await session.flush()
    if status == AttendanceStatus.GOING:
        await timeline.record_event_going(session, event.id, actor.id, now)
    else:
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(actor.id))
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.EVENT_CREATE,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before=None,
        after={
            "slug": event.slug,
            "kind": event.kind,
            "title": event.title,
            "starts_at": event.starts_at.isoformat(),
            "ends_at": event.ends_at.isoformat(),
            "formation_enabled": event.formation_enabled,
            "review_reasons": list(event.review_reasons),
        },
        reason=None,
    )
    return event


def json_value(value: Any) -> JsonValue:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal | UUID | Enum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_value(item) for item in value]
    return value


def change_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


async def update_member_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    body: schemas.UpdateMemberEventBody,
    actor: models.User,
    now: datetime,
) -> models.Event:
    event = await catalog.visible_event(session, event_id, actor)
    event = await catalog.load_event(session, event_id, for_update=True)
    if event.origin != EventOrigin.MEMBER.value:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if not actor.is_admin and event.created_by != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if not actor.is_admin and now >= event.starts_at:
        raise ProblemError(ErrorCode.INVALID_STATE, "Editing closes at the event start")
    raw = body.model_dump(exclude_unset=True)
    for field in ("timezone", "kind", "title", "starts_at", "ends_at", "format", "area_keys"):
        if field in raw and raw[field] is None:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"{field} cannot be null")
    if "location" in raw:
        raw["location"] = (
            (body.location.model_dump(mode="json", exclude_none=True) or None)
            if body.location is not None
            else None
        )
    if "website_url" in raw:
        raw["website_url"] = str(body.website_url) if body.website_url is not None else None
    resolved: list[tuple[models.Area, catalog.EventAreaInput]] = []
    if body.area_keys is not None:
        inputs = [
            catalog.EventAreaInput(key=key, importance=AreaImportance.PRIMARY)
            for key in body.area_keys
        ]
        resolved = await catalog.resolve_areas(session, inputs)
        raw["area_keys"] = list(body.area_keys)
    starts = raw.get("starts_at", event.starts_at)
    ends = raw.get("ends_at", event.ends_at)
    if ends < starts:
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "ends_at must not precede starts_at")
    changed: dict[str, tuple[Any, Any]] = {}
    for field, after in raw.items():
        if field == "area_keys":
            before: Any = list(
                await session.scalars(
                    select(models.Area.key)
                    .join(models.EventArea, models.EventArea.area_id == models.Area.id)
                    .where(models.EventArea.event_id == event.id)
                    .order_by(models.Area.sort, models.Area.key)
                )
            )
        else:
            before = getattr(event, field)
        if json_value(before) != json_value(after):
            changed[field] = (before, after)
    if not changed:
        await session.flush()
        return event
    previous_dates = formation.event_dates(event)
    for field, (_, after) in changed.items():
        if field == "area_keys":
            await session.execute(
                delete(models.EventArea).where(models.EventArea.event_id == event.id)
            )
            session.add_all(
                models.EventArea(
                    event_id=event.id,
                    area_id=area.id,
                    importance=area_input.importance.value,
                    headcount=area_input.headcount,
                )
                for area, area_input in resolved
            )
        elif field in ("kind", "format"):
            setattr(event, field, after.value)
        else:
            setattr(event, field, after)
    session.add(
        models.EventRevision(
            event_id=event.id,
            origin=EventRevisionOrigin.MEMBER.value,
            actor_id=actor.id,
            diff={
                field: {"before": json_value(before), "after": json_value(after)}
                for field, (before, after) in changed.items()
            },
            ignored_fields=[],
        )
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN if actor.is_admin else ActorKind.USER,
        action=AuditAction.EVENT_EDIT,
        entity_type=AuditEntityType.EVENT,
        entity_id=event.id,
        before={field: json_value(before) for field, (before, _) in changed.items()},
        after={field: json_value(after) for field, (_, after) in changed.items()},
        reason=None,
    )
    notifiable = sorted({"starts_at", "ends_at", "format"} & changed.keys())
    if notifiable:
        changes = [
            NotificationChange(
                field=field,
                previous=change_text(changed[field][0]),
                current=change_text(changed[field][1]),
            )
            for field in notifiable
        ]
        await catalog.notify_event_changed(session, event, changes, now)
    if {"starts_at", "ends_at"} & changed.keys():
        await formation.dates_changed(session, jobs, event, previous_dates, now)
    await session.flush()
    await session.refresh(event)
    return event


async def cancel_member_event(
    session: AsyncSession,
    jobs: procrastinate.App,
    event_id: UUID,
    reason: str,
    actor: models.User,
    now: datetime,
) -> models.Event:
    event = await catalog.visible_event(session, event_id, actor)
    if event.origin != EventOrigin.MEMBER.value:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if not actor.is_admin and event.created_by != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    event = await catalog.cancel_event(
        session,
        jobs,
        event_id,
        reason=reason,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN if actor.is_admin else ActorKind.USER,
        now=now,
    )
    attended = list(
        await session.scalars(
            select(models.EventAttendance.user_id).where(
                models.EventAttendance.event_id == event.id,
                models.EventAttendance.status == AttendanceStatus.ATTENDED.value,
            )
        )
    )
    await catalog.notify_event_cancelled(session, event, attended, reason, now)
    await timeline.hide_feed_subject(session, "event", event.id, now)
    return event
