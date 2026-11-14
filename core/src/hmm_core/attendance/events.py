from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import AttendanceStatus, ConfirmationFactType, ErrorCode, Trust
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import schemas
from hmm_core.attendance.service import attendance_view
from hmm_core.catalog import service as catalog
from hmm_core.feed import confirmations, digests, timeline
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.pagination import CursorParams, apply_keyset, encode_cursor
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
