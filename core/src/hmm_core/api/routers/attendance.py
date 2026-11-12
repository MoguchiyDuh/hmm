from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import AttendanceStatus, EventId, Page

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.attendance import (
    Attendance,
    CancelMemberEventBody,
    CreateMemberEventBody,
    MemberEvent,
    SetAttendanceBody,
    UpdateMemberEventBody,
)
from hmm_core.api.security import MemberAuth

router = APIRouter(tags=["attendance"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["attendance"], responses=PROBLEM_RESPONSES)


@router.put(
    "/events/{id}/attendance", status_code=status.HTTP_200_OK, summary="Set own event attendance"
)
async def set_attendance(id: EventId, body: SetAttendanceBody, actor: MemberAuth) -> Attendance:
    not_implemented()


@router.delete(
    "/events/{id}/attendance",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove own event attendance",
)
async def delete_attendance(id: EventId, actor: MemberAuth) -> None:
    not_implemented()


@router.get(
    "/events/{id}/attendees",
    status_code=status.HTTP_200_OK,
    summary="List event attendees with role and trust",
)
async def list_attendees(
    id: EventId,
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    attendance_status: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
) -> Page[Attendance]:
    not_implemented()


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
    attendance_status: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
) -> list[Attendance]:
    not_implemented()
