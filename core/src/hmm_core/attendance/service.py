from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    AttendanceRole,
    AttendanceStatus,
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    EventKind,
    Trust,
)
from pydantic import HttpUrl, TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import schemas
from hmm_core.feed import confirmations
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.schemas import EventRef, UserRef
from hmm_core.platform.settings import load_settings


def attendance_view(
    attendance: models.EventAttendance,
    event: models.Event,
    owner: models.User,
    actor: models.User,
    *,
    can_confirm: bool = False,
) -> schemas.Attendance:
    actions: list[schemas.AttendanceAction] = []
    if actor.id == owner.id and attendance.status != AttendanceStatus.CANCELLED:
        actions = [schemas.AttendanceAction.EDIT, schemas.AttendanceAction.REMOVE]
    elif can_confirm and attendance.status == AttendanceStatus.ATTENDED:
        actions = [schemas.AttendanceAction.CONFIRM]
    return schemas.Attendance(
        id=attendance.id,
        event_id=attendance.event_id,
        user_id=attendance.user_id,
        event=EventRef(
            id=event.id,
            title=event.title,
            kind=EventKind(event.kind),
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            timezone=event.timezone,
        ),
        user=UserRef(id=owner.id, display_name=owner.display_name, avatar_url=owner.avatar_url),
        status=AttendanceStatus(attendance.status),
        role=AttendanceRole(attendance.role),
        proof_links=TypeAdapter(list[HttpUrl]).validate_python(attendance.proof_links),
        note=attendance.note,
        trust=Trust(attendance.trust),
        status_at=attendance.status_at,
        created_at=attendance.created_at,
        updated_at=attendance.updated_at,
        allowed_actions=actions,
    )


async def sync_going(
    session: AsyncSession, event_id: UUID, user_id: UUID, *, going: bool, now: datetime
) -> None:
    attendance = await session.scalar(
        select(models.EventAttendance).where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == user_id,
        )
    )
    if going:
        if attendance is None:
            session.add(
                models.EventAttendance(
                    event_id=event_id,
                    user_id=user_id,
                    status=AttendanceStatus.GOING.value,
                    role=AttendanceRole.ATTENDEE.value,
                    proof_links=[],
                    note=None,
                    trust=Trust.SELF.value,
                    status_at=now,
                )
            )
            await session.flush()
        elif attendance.status == AttendanceStatus.CANCELLED:
            attendance.status = AttendanceStatus.GOING.value
            attendance.status_at = now
        return None
    if attendance is not None and attendance.status == AttendanceStatus.GOING:
        attendance.status = AttendanceStatus.CANCELLED.value
        attendance.status_at = now
    return None


async def ensure_attended(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    event_id: UUID,
    user_id: UUID,
    now: datetime,
    role: AttendanceRole = AttendanceRole.ATTENDEE,
    system_confirmation: bool = True,
) -> models.EventAttendance:
    event = await session.scalar(
        select(models.Event).where(models.Event.id == event_id).with_for_update()
    )
    if event is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    attendance = await session.scalar(
        select(models.EventAttendance)
        .where(
            models.EventAttendance.event_id == event_id,
            models.EventAttendance.user_id == user_id,
        )
        .with_for_update()
    )
    changed = False
    if attendance is None:
        attendance = models.EventAttendance(
            event_id=event_id,
            user_id=user_id,
            status=AttendanceStatus.ATTENDED.value,
            role=role.value,
            proof_links=[],
            note=None,
            trust=Trust.SELF.value,
            status_at=now,
        )
        session.add(attendance)
        await session.flush()
        changed = True
    elif attendance.status != AttendanceStatus.ATTENDED:
        attendance.status = AttendanceStatus.ATTENDED.value
        attendance.status_at = now
        changed = True
    if system_confirmation:
        await confirmations.add_confirmation(
            session,
            jobs,
            fact_type=ConfirmationFactType.ATTENDANCE,
            fact_id=attendance.id,
            subject_user_id=user_id,
            confirmer_id=None,
            source=ConfirmationSource.SYSTEM,
            now=now,
        )
    elif changed:
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return attendance


async def expire_going_attendances(
    session: AsyncSession, now: datetime, *, limit: int = 500
) -> int:
    settings = await load_settings(session)
    cutoff = now - timedelta(days=settings.attendance_claim_days)
    ids = list(
        await session.scalars(
            select(models.EventAttendance.id)
            .join(models.Event, models.Event.id == models.EventAttendance.event_id)
            .where(
                models.EventAttendance.status == AttendanceStatus.GOING.value,
                models.Event.ends_at < cutoff,
            )
            .order_by(models.Event.ends_at, models.EventAttendance.id)
            .limit(limit)
        )
    )
    if not ids:
        return 0
    await session.execute(
        update(models.EventAttendance)
        .where(models.EventAttendance.id.in_(ids))
        .values(status=AttendanceStatus.CANCELLED.value, status_at=now)
    )
    return len(ids)
