from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from hmm_contracts.common import AttendanceStatus, EventId

from hmm_core.attendance import events
from hmm_core.attendance.schemas import (
    Attendance,
    AttendancePage,
    CancelMemberEventBody,
    CreateMemberEventBody,
    MemberEvent,
    SetAttendanceBody,
    UpdateMemberEventBody,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import MemberAuth

router = APIRouter(tags=["attendance"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["attendance"], responses=PROBLEM_RESPONSES)


@router.put(
    "/events/{id}/attendance", status_code=status.HTTP_200_OK, summary="Set own event attendance"
)
async def set_attendance(
    id: EventId,
    body: SetAttendanceBody,
    actor: MemberAuth,
    session: Session,
    request: Request,
    clock: CurrentClock,
) -> Attendance:
    now = clock.now()
    return await events.set_attendance(session, request.app.state.jobs, id, body, actor, now)


@router.delete(
    "/events/{id}/attendance",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove own event attendance",
)
async def delete_attendance(
    id: EventId, actor: MemberAuth, session: Session, request: Request, clock: CurrentClock
) -> Response:
    now = clock.now()
    await events.delete_attendance(session, request.app.state.jobs, id, actor, now)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/events/{id}/attendees",
    status_code=status.HTTP_200_OK,
    summary="List event attendees with role and trust",
)
async def list_attendees(
    id: EventId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    attendance_status: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
) -> AttendancePage:
    now = clock.now()
    return await events.list_attendees(
        session, id, actor, pagination, status=attendance_status, now=now
    )


@router.post("/events", status_code=status.HTTP_201_CREATED, summary="Create a member event")
async def create_event(
    body: CreateMemberEventBody,
    actor: MemberAuth,
    confirm_new: Annotated[bool, Query()] = False,
) -> MemberEvent:
    not_implemented()


@router.patch(
    "/events/{id}",
    status_code=status.HTTP_200_OK,
    summary="Edit event as creator before its start or as admin",
)
async def update_event(id: EventId, body: UpdateMemberEventBody, actor: MemberAuth) -> MemberEvent:
    not_implemented()


@router.post(
    "/events/{id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel event as creator or admin",
)
async def cancel_event(id: EventId, body: CancelMemberEventBody, actor: MemberAuth) -> MemberEvent:
    not_implemented()


@router.get("/me/attendances", status_code=status.HTTP_200_OK, summary="List own attendances")
async def list_my_attendances(
    actor: MemberAuth,
    session: Session,
    attendance_status: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
) -> list[Attendance]:
    return await events.list_my_attendances(session, actor, status=attendance_status)
