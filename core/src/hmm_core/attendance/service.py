from datetime import datetime
from uuid import UUID

from hmm_contracts.common import AttendanceRole, AttendanceStatus, EventKind, Trust
from pydantic import HttpUrl, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.attendance import schemas
from hmm_core.platform.schemas import EventRef, UserRef


def attendance_view(
    attendance: models.EventAttendance,
    event: models.Event,
    owner: models.User,
    actor: models.User,
) -> schemas.Attendance:
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
        allowed_actions=[schemas.AttendanceAction.EDIT, schemas.AttendanceAction.REMOVE]
        if actor.id == owner.id and attendance.status != AttendanceStatus.CANCELLED
        else [],
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
