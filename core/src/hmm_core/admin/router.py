from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from hmm_contracts.common import OnboardingStatus, SubjectId, UserId
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.admin import service as admin
from hmm_core.admin.schemas import (
    AdminDashboard,
    AdminUser,
    AdminUserAction,
    AdminUserPage,
    AuditEntryPage,
    BanUserBody,
    Job,
    RetryJobBody,
    Setting,
    SystemStatus,
    UnbanUserBody,
    UpdateSettingBody,
    UpdateUserBody,
)
from hmm_core.identity import service as identity
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import AdminAuth
from hmm_core.platform.settings import SettingKey

router = APIRouter(tags=["admin"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["admin"], responses=PROBLEM_RESPONSES)


async def admin_user(session: AsyncSession, user: models.User) -> AdminUser:
    await identity.refresh_user(session, user)
    allowed = {
        AdminUserAction.EDIT,
        AdminUserAction.RECOMPUTE,
        AdminUserAction.ADJUST_XP,
        AdminUserAction.OVERRIDE_SCORE,
    }
    if user.banned_at is not None:
        allowed.add(AdminUserAction.UNBAN)
    elif not user.is_admin:
        allowed |= {AdminUserAction.BAN, AdminUserAction.GRANT_ADMIN}
    elif await identity.admin_count(session) > 1:
        allowed.add(AdminUserAction.REVOKE_ADMIN)
    if user.onboarding_status == OnboardingStatus.COMPLETED:
        allowed.add(AdminUserAction.REOPEN_ONBOARDING)
    return AdminUser(
        id=user.id,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        github_id=user.github_id,
        github_login=user.github_login,
        about_md=user.about_md,
        is_admin=user.is_admin,
        banned_at=user.banned_at,
        ban_reason=user.ban_reason,
        onboarding_status=user.onboarding_status,
        last_seen_at=user.last_seen_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        allowed_actions=[action for action in AdminUserAction if action in allowed],
    )


@router.get(
    "/admin/dashboard",
    status_code=status.HTTP_200_OK,
    summary="Get exception queue and operational counters",
)
async def get_dashboard(actor: AdminAuth, session: Session, clock: CurrentClock) -> AdminDashboard:
    return await admin.dashboard(session, clock.now())


@router.get("/admin/users", status_code=status.HTTP_200_OK, summary="List users")
async def list_users(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    session: Session,
    q: Annotated[str | None, Query()] = None,
    banned: Annotated[bool | None, Query()] = None,
    is_admin: Annotated[bool | None, Query(alias="admin")] = None,
) -> AdminUserPage:
    users, next_cursor = await admin.list_users(session, pagination, q, banned, is_admin)
    return AdminUserPage(
        items=[await admin_user(session, user) for user in users], next_cursor=next_cursor
    )


@router.patch(
    "/admin/users/{id}",
    status_code=status.HTTP_200_OK,
    summary="Update administrator flag and profile fields",
)
async def update_user(
    id: UserId, body: UpdateUserBody, actor: AdminAuth, session: Session
) -> AdminUser:
    target = await identity.lock_user(session, id)
    changes = {
        key: value
        for key, value in (("display_name", body.display_name), ("about_md", body.about_md))
        if value is not None
    }
    await identity.edit_user(session, target, changes, actor.id, body.reason)
    if body.is_admin is not None:
        await identity.set_admin(session, target, body.is_admin, actor.id, body.reason)
    return await admin_user(session, target)


@router.post(
    "/admin/users/{id}/ban", status_code=status.HTTP_200_OK, summary="Ban user with a reason"
)
async def ban_user(
    id: UserId, body: BanUserBody, actor: AdminAuth, session: Session, clock: CurrentClock
) -> AdminUser:
    target = await identity.lock_user(session, id)
    await identity.ban_user(session, target, body.reason, actor.id, clock.now())
    return await admin_user(session, target)


@router.post("/admin/users/{id}/unban", status_code=status.HTTP_200_OK, summary="Unban user")
async def unban_user(
    id: UserId, actor: AdminAuth, session: Session, body: UnbanUserBody | None = None
) -> AdminUser:
    target = await identity.lock_user(session, id)
    await identity.unban_user(session, target, actor.id, body.reason if body else None)
    return await admin_user(session, target)


@router.get("/admin/audit", status_code=status.HTTP_200_OK, summary="List audit log")
async def list_audit_entries(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    session: Session,
    actor_id: Annotated[UserId | None, Query(alias="actor")] = None,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[SubjectId | None, Query()] = None,
) -> AuditEntryPage:
    return await admin.list_audit_entries(session, pagination, actor_id, entity_type, entity_id)


@router.get(
    "/admin/settings",
    status_code=status.HTTP_200_OK,
    summary="List runtime settings with defaults and descriptions",
)
async def list_settings(actor: AdminAuth, session: Session) -> list[Setting]:
    return await admin.list_settings(session)


@router.put(
    "/admin/settings/{key}",
    status_code=status.HTTP_200_OK,
    summary="Update runtime setting validated against its key schema",
)
async def update_setting(
    key: SettingKey, body: UpdateSettingBody, actor: AdminAuth, session: Session
) -> Setting:
    return await admin.update_setting(session, key, body.value, actor.id, body.reason)


@router.get(
    "/admin/system",
    status_code=status.HTTP_200_OK,
    summary="Get parser heartbeats, job queues, algo version and failures",
)
async def get_system_status(actor: AdminAuth) -> SystemStatus:
    not_implemented()


@router.post(
    "/admin/jobs/{id}/retry", status_code=status.HTTP_202_ACCEPTED, summary="Retry a failed job"
)
async def retry_job(
    id: Annotated[int, Path(ge=1)],
    actor: AdminAuth,
    body: RetryJobBody | None = None,
) -> Job:
    not_implemented()
