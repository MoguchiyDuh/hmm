from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from hmm_contracts.common import AreaKey, Page, UserId

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.profiles import (
    Availability,
    Cv,
    CvUpload,
    NotificationSettings,
    UpdateAvailabilityBody,
    UpdateNotificationSettingsBody,
    UpdateProfileBody,
    UploadCvBody,
    UserProfile,
    UserSummary,
)
from hmm_core.api.security import MemberAuth, UserAuth

router = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["profiles"], responses=PROBLEM_RESPONSES)


@router.get("/users", status_code=status.HTTP_200_OK, summary="Member directory")
async def list_users(
    actor: MemberAuth,
    pagination: Annotated[CursorParams, Depends()],
    q: Annotated[str | None, Query()] = None,
    area: Annotated[AreaKey | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=10)] = None,
    available: Annotated[bool | None, Query()] = None,
    mentor: Annotated[bool | None, Query()] = None,
    sort: Annotated[Literal["xp", "activity", "score", "reputation", "name"], Query()] = "xp",
) -> Page[UserSummary]:
    not_implemented()


@router.get("/users/{id}", status_code=status.HTTP_200_OK, summary="Public profile")
async def get_user(id: UserId, actor: MemberAuth) -> UserProfile:
    not_implemented()


@router.patch("/me/profile", status_code=status.HTTP_200_OK, summary="Edit profile fields")
async def update_profile(body: UpdateProfileBody, actor: UserAuth) -> UserProfile:
    not_implemented()


@router.put("/me/availability", status_code=status.HTTP_200_OK, summary="Set do-not-disturb")
async def update_availability(body: UpdateAvailabilityBody, actor: MemberAuth) -> Availability:
    not_implemented()


@router.post(
    "/me/github/refresh", status_code=status.HTTP_202_ACCEPTED, summary="Enqueue GitHub sync"
)
async def refresh_github(actor: MemberAuth) -> None:
    not_implemented()


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
async def upload_cv(request: Request, actor: UserAuth) -> CvUpload:
    not_implemented()


@router.delete("/me/cv", status_code=status.HTTP_204_NO_CONTENT, summary="Remove CV")
async def delete_cv(actor: UserAuth) -> None:
    not_implemented()


@router.get("/users/{id}/cv", status_code=status.HTTP_200_OK, summary="Get short-lived CV download")
async def get_cv(id: UserId, actor: MemberAuth) -> Cv:
    not_implemented()


@router.get(
    "/me/notification-settings", status_code=status.HTTP_200_OK, summary="Get notification settings"
)
async def get_notification_settings(
    actor: UserAuth,
) -> NotificationSettings:
    not_implemented()


@router.put(
    "/me/notification-settings",
    status_code=status.HTTP_200_OK,
    summary="Update notification settings",
)
async def update_notification_settings(
    body: UpdateNotificationSettingsBody, actor: UserAuth
) -> NotificationSettings:
    not_implemented()
