from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from hmm_contracts.common import AreaKey, ErrorCode, UserId
from starlette.datastructures import UploadFile

from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, ProblemError
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import MemberAuth, UserAuth
from hmm_core.profiles import service
from hmm_core.profiles.schemas import (
    Availability,
    Cv,
    CvAction,
    CvUpload,
    NotificationSettings,
    UpdateAvailabilityBody,
    UpdateNotificationSettingsBody,
    UpdateProfileBody,
    UploadCvBody,
    UserProfile,
    UserSummaryPage,
)

router = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)


async def read_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(service.CHUNK_SIZE):
        yield chunk


@router.get("/users", status_code=status.HTTP_200_OK, summary="Member directory")
async def list_users(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    session: Session,
    clock: CurrentClock,
    q: Annotated[str | None, Query()] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=10)] = None,
    available: Annotated[bool | None, Query()] = None,
    mentor: Annotated[bool | None, Query()] = None,
    sort: Annotated[Literal["xp", "activity", "score", "reputation", "name"], Query()] = "xp",
) -> UserSummaryPage:
    query = service.DirectoryQuery(
        q=q,
        area_key=area,
        min_score=min_score,
        available=available,
        mentor=mentor,
        sort=sort,
    )
    now = clock.now()
    users, next_cursor = await service.list_directory(session, pagination, query, now)
    items = await service.user_summaries(session, users, actor, now)
    return UserSummaryPage(items=items, next_cursor=next_cursor)


@router.get("/users/{id}", status_code=status.HTTP_200_OK, summary="Public profile")
async def get_user(
    id: UserId, actor: MemberAuth, session: Session, clock: CurrentClock
) -> UserProfile:
    user = await service.get_user(session, id)
    return await service.user_profile(session, user, actor, clock.now())


@router.patch("/me/profile", status_code=status.HTTP_200_OK, summary="Edit profile fields")
async def update_profile(
    body: UpdateProfileBody,
    actor: UserAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> UserProfile:
    target = await service.update_profile(session, request.app.state.jobs, actor, body)
    return await service.user_profile(session, target, actor, clock.now())


@router.put("/me/availability", status_code=status.HTTP_200_OK, summary="Set do-not-disturb")
async def update_availability(
    body: UpdateAvailabilityBody, actor: MemberAuth, session: Session
) -> Availability:
    user = await service.set_availability(session, actor, body.dnd_until)
    return service.availability_view(user)


@router.post(
    "/me/github/refresh", status_code=status.HTTP_202_ACCEPTED, summary="Enqueue GitHub sync"
)
async def refresh_github(
    actor: MemberAuth, request: Request, session: Session, clock: CurrentClock
) -> None:
    await service.refresh_github(session, request.app.state.jobs, user_id=actor.id, now=clock.now())


@router.put(
    "/me/cv",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload CV",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": UploadCvBody.model_json_schema()}},
        }
    },
)
async def upload_cv(
    request: Request, actor: UserAuth, session: Session, clock: CurrentClock
) -> CvUpload:
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Expected a file field named 'file'")
    row = await service.upload_cv(
        session,
        request.app.state.jobs,
        service.require_storage(request.app.state.storage),
        user_id=actor.id,
        filename=upload.filename,
        chunks=read_chunks(upload),
        now=clock.now(),
    )
    return CvUpload(
        filename=row.filename,
        text_status=row.text_status,
        allowed_actions=[CvAction.REPLACE, CvAction.DELETE],
    )


@router.delete("/me/cv", status_code=status.HTTP_204_NO_CONTENT, summary="Remove CV")
async def delete_cv(
    actor: UserAuth, request: Request, session: Session, clock: CurrentClock
) -> None:
    await service.delete_cv(
        session,
        request.app.state.jobs,
        service.require_storage(request.app.state.storage),
        user_id=actor.id,
        now=clock.now(),
    )


@router.get("/users/{id}/cv", status_code=status.HTTP_200_OK, summary="Get short-lived CV download")
async def get_cv(id: UserId, actor: MemberAuth, request: Request, session: Session) -> Cv:
    row, url = await service.cv_download(
        session, service.require_storage(request.app.state.storage), actor=actor, user_id=id
    )
    privileged = actor.id == id or actor.is_admin
    return Cv(
        filename=row.filename,
        url=url,
        text_status=row.text_status if privileged else None,
        text_error=row.text_error if privileged else None,
        allowed_actions=[CvAction.REPLACE, CvAction.DELETE] if actor.id == id else [],
    )


@router.get(
    "/me/notification-settings", status_code=status.HTTP_200_OK, summary="Get notification settings"
)
async def get_notification_settings(actor: UserAuth, session: Session) -> NotificationSettings:
    return await service.notification_settings(session, actor.id)


@router.put(
    "/me/notification-settings",
    status_code=status.HTTP_200_OK,
    summary="Update notification settings",
)
async def update_notification_settings(
    body: UpdateNotificationSettingsBody, actor: UserAuth, session: Session
) -> NotificationSettings:
    return await service.update_notification_settings(session, actor.id, body)
