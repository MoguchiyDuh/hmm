import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import AdminAlertId, AlertStatus, NotificationId
from hmm_contracts.notifications import (
    AlertKind,
    DeliveryAck,
    PendingDeliveriesQuery,
    PendingDelivery,
)

from hmm_core.notifications import service
from hmm_core.notifications.schemas import (
    Alert,
    AlertPage,
    InboxNotificationPage,
    ResolveAlertBody,
    UnreadCount,
)
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, not_implemented
from hmm_core.platform.pagination import CursorParams
from hmm_core.platform.security import AdminAuth, BotAuth, UserAuth

router = APIRouter(tags=["notifications"], responses=PROBLEM_RESPONSES)
internal = APIRouter(tags=["notifications"], responses=PROBLEM_RESPONSES)


@router.get("/notifications", status_code=status.HTTP_200_OK, summary="List notification inbox")
async def list_notifications(
    pagination: Annotated[CursorParams, Depends()],
    actor: UserAuth,
    session: Session,
    unread: Annotated[bool | None, Query()] = None,
) -> InboxNotificationPage:
    return await service.list_notifications(session, actor.id, pagination, unread)


@router.get(
    "/notifications/unread-count",
    status_code=status.HTTP_200_OK,
    summary="Get unread notification count",
)
async def get_unread_count(actor: UserAuth, session: Session) -> UnreadCount:
    return await service.unread_count(session, actor.id)


@router.post(
    "/notifications/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark all notifications read",
)
async def read_all_notifications(actor: UserAuth, session: Session, clock: CurrentClock) -> None:
    await service.read_all_notifications(session, actor.id, clock.now())


@router.post(
    "/notifications/{id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark notification read",
)
async def read_notification(
    id: NotificationId, actor: UserAuth, session: Session, clock: CurrentClock
) -> None:
    await service.read_notification(session, actor.id, id, clock.now())


@internal.get(
    "/notifications/pending",
    status_code=status.HTTP_200_OK,
    summary="Long-poll Telegram deliveries with a 60-second lease",
)
async def list_pending_deliveries(
    query: Annotated[PendingDeliveriesQuery, Query()],
    actor: BotAuth,
    session: Session,
    clock: CurrentClock,
) -> list[PendingDelivery]:
    deadline = time.monotonic() + query.wait
    while True:
        leased = await service.lease_deliveries(session, clock.now(), query.limit)
        if leased or time.monotonic() >= deadline:
            return leased
        await asyncio.sleep(1)


@internal.post(
    "/notifications/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Acknowledge Telegram delivery outcomes",
)
async def acknowledge_deliveries(
    body: DeliveryAck, actor: BotAuth, session: Session, clock: CurrentClock
) -> None:
    await service.acknowledge(session, clock.now(), body.results)


@router.get("/admin/alerts", status_code=status.HTTP_200_OK, summary="List admin alerts")
async def list_alerts(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    status: Annotated[AlertStatus | None, Query()] = None,
    type: Annotated[AlertKind | None, Query()] = None,
) -> AlertPage:
    not_implemented()


@router.post(
    "/admin/alerts/{id}/resolve", status_code=status.HTTP_200_OK, summary="Resolve admin alert"
)
async def resolve_alert(id: AdminAlertId, body: ResolveAlertBody, actor: AdminAuth) -> Alert:
    not_implemented()
