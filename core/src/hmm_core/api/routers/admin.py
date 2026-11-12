from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from hmm_contracts.common import Page, SubjectId, UserId

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.admin import (
    AdminDashboard,
    AdminUser,
    AuditEntry,
    BanUserBody,
    Job,
    RetryJobBody,
    Setting,
    SystemStatus,
    UnbanUserBody,
    UpdateSettingBody,
    UpdateUserBody,
)
from hmm_core.api.security import admin
from hmm_core.settings import SettingKey

router = APIRouter(tags=["admin"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["admin"], responses=PROBLEM_RESPONSES)


@router.get(
    "/admin/dashboard",
    status_code=status.HTTP_200_OK,
    summary="Get exception queue and operational counters",
)
async def get_dashboard(actor: Annotated[None, Depends(admin)]) -> AdminDashboard:
    not_implemented()


@router.get("/admin/users", status_code=status.HTTP_200_OK, summary="List users")
async def list_users(
    pagination: Annotated[CursorParams, Depends()],
    actor: Annotated[None, Depends(admin)],
    q: Annotated[str | None, Query()] = None,
    banned: Annotated[bool | None, Query()] = None,
    is_admin: Annotated[bool | None, Query(alias="admin")] = None,
) -> Page[AdminUser]:
    not_implemented()


@router.patch(
    "/admin/users/{id}",
    status_code=status.HTTP_200_OK,
    summary="Update administrator flag and profile fields",
)
async def update_user(
    id: UserId, body: UpdateUserBody, actor: Annotated[None, Depends(admin)]
) -> AdminUser:
    not_implemented()


@router.post(
    "/admin/users/{id}/ban", status_code=status.HTTP_200_OK, summary="Ban user with a reason"
)
async def ban_user(
    id: UserId, body: BanUserBody, actor: Annotated[None, Depends(admin)]
) -> AdminUser:
    not_implemented()


@router.post("/admin/users/{id}/unban", status_code=status.HTTP_200_OK, summary="Unban user")
async def unban_user(
    id: UserId, actor: Annotated[None, Depends(admin)], body: UnbanUserBody | None = None
) -> AdminUser:
    not_implemented()


@router.get("/admin/audit", status_code=status.HTTP_200_OK, summary="List audit log")
async def list_audit_entries(
    pagination: Annotated[CursorParams, Depends()],
    actor_user: Annotated[None, Depends(admin)],
    actor: Annotated[UserId | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[SubjectId | None, Query()] = None,
) -> Page[AuditEntry]:
    not_implemented()


@router.get(
    "/admin/settings",
    status_code=status.HTTP_200_OK,
    summary="List runtime settings with defaults and descriptions",
)
async def list_settings(actor: Annotated[None, Depends(admin)]) -> list[Setting]:
    not_implemented()


@router.put(
    "/admin/settings/{key}",
    status_code=status.HTTP_200_OK,
    summary="Update runtime setting validated against its key schema",
)
async def update_setting(
    key: SettingKey, body: UpdateSettingBody, actor: Annotated[None, Depends(admin)]
) -> Setting:
    not_implemented()


@router.get(
    "/admin/system",
    status_code=status.HTTP_200_OK,
    summary="Get parser heartbeats, job queues, algo version and failures",
)
async def get_system_status(actor: Annotated[None, Depends(admin)]) -> SystemStatus:
    not_implemented()


@router.post(
    "/admin/jobs/{id}/retry", status_code=status.HTTP_202_ACCEPTED, summary="Retry a failed job"
)
async def retry_job(
    id: Annotated[int, Path(ge=1)],
    actor: Annotated[None, Depends(admin)],
    body: RetryJobBody | None = None,
) -> Job:
    not_implemented()
