from hmm_contracts.common import AttendanceRole, AttendanceStatus, EventKind, Trust
from pydantic import HttpUrl, TypeAdapter

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
