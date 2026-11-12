from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import AdminAlertId, AlertStatus, NotificationId, Page
from hmm_contracts.notifications import (
    AlertKind,
    DeliveryAck,
    PendingDeliveriesQuery,
    PendingDelivery,
    TelegramLinkRequest,
    TelegramLinkResult,
    TelegramUnlinkRequest,
)

from hmm_core.api.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.notifications import (
    Alert,
    InboxNotification,
    ResolveAlertBody,
    UnreadCount,
)
from hmm_core.api.security import admin, bot_service, session_user

router = APIRouter(tags=["notifications"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["notifications"], responses=PROBLEM_RESPONSES)


@router.get("/notifications", status_code=status.HTTP_200_OK, summary="List notification inbox")
async def list_notifications(
    pagination: Annotated[CursorParams, Depends()],
    actor: Annotated[None, Depends(session_user)],
    unread: Annotated[bool | None, Query()] = None,
) -> Page[InboxNotification]:
    not_implemented()


@router.get(
    "/notifications/unread-count",
    status_code=status.HTTP_200_OK,
    summary="Get unread notification count",
)
async def get_unread_count(actor: Annotated[None, Depends(session_user)]) -> UnreadCount:
    not_implemented()


@router.post(
    "/notifications/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark all notifications read",
)
async def read_all_notifications(actor: Annotated[None, Depends(session_user)]) -> None:
    not_implemented()


@router.post(
    "/notifications/{id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark notification read",
)
async def read_notification(
    id: NotificationId, actor: Annotated[None, Depends(session_user)]
) -> None:
    not_implemented()


@internal.get(
    "/notifications/pending",
    status_code=status.HTTP_200_OK,
    summary="Long-poll Telegram deliveries with a 60-second lease",
)
async def list_pending_deliveries(
    query: Annotated[PendingDeliveriesQuery, Query()], actor: Annotated[None, Depends(bot_service)]
) -> list[PendingDelivery]:
    not_implemented()


@internal.post(
    "/notifications/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Acknowledge Telegram delivery outcomes",
)
async def acknowledge_deliveries(
    body: DeliveryAck, actor: Annotated[None, Depends(bot_service)]
) -> None:
    not_implemented()


@internal.post(
    "/telegram/link",
    status_code=status.HTTP_200_OK,
    summary="Link Telegram using a one-time account code",
)
async def link_telegram(
    body: TelegramLinkRequest, actor: Annotated[None, Depends(bot_service)]
) -> TelegramLinkResult:
    not_implemented()


@internal.post(
    "/telegram/unlink", status_code=status.HTTP_204_NO_CONTENT, summary="Unlink Telegram account"
)
async def unlink_telegram(
    body: TelegramUnlinkRequest, actor: Annotated[None, Depends(bot_service)]
) -> None:
    not_implemented()


@router.get("/admin/alerts", status_code=status.HTTP_200_OK, summary="List admin alerts")
async def list_alerts(
    pagination: Annotated[CursorParams, Depends()],
    actor: Annotated[None, Depends(admin)],
    status: Annotated[AlertStatus | None, Query()] = None,
    type: Annotated[AlertKind | None, Query()] = None,
) -> Page[Alert]:
    not_implemented()


@router.post(
    "/admin/alerts/{id}/resolve", status_code=status.HTTP_200_OK, summary="Resolve admin alert"
)
async def resolve_alert(
    id: AdminAlertId, body: ResolveAlertBody, actor: Annotated[None, Depends(admin)]
) -> Alert:
    not_implemented()
