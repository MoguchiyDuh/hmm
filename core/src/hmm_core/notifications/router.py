import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from hmm_contracts.common import (
    ActorKind,
    AdminAlertId,
    AlertStatus,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    NotificationId,
)
from hmm_contracts.notifications import (
    AlertKind,
    DeliveryAck,
    PendingDeliveriesQuery,
    PendingDelivery,
)
from sqlalchemy import select

from hmm_core import models
from hmm_core.notifications import service
from hmm_core.notifications.schemas import (
    Alert,
    AlertAction,
    AlertPage,
    InboxNotificationPage,
    ResolveAlertBody,
    UnreadCount,
)
from hmm_core.notifications.service import alert_contract
from hmm_core.platform.audit import record_audit
from hmm_core.platform.clock import CurrentClock
from hmm_core.platform.db import Session
from hmm_core.platform.errors import PROBLEM_RESPONSES, ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
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


def serialize_alert(row: models.AdminAlert) -> Alert:
    return Alert(
        **alert_contract(row).model_dump(),
        allowed_actions=[AlertAction.RESOLVE] if row.status == AlertStatus.OPEN else [],
    )


@router.get("/admin/alerts", status_code=status.HTTP_200_OK, summary="List admin alerts")
async def list_alerts(
    pagination: Annotated[CursorParams, Depends()],
    actor: AdminAuth,
    session: Session,
    status: Annotated[AlertStatus | None, Query()] = None,
    type: Annotated[AlertKind | None, Query()] = None,
) -> AlertPage:
    statement = select(models.AdminAlert)
    if status is not None:
        statement = statement.where(models.AdminAlert.status == status.value)
    if type is not None:
        statement = statement.where(models.AdminAlert.type == type.value)
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.AdminAlert.created_at],
        id_column=models.AdminAlert.id,
        serialize=serialize_alert,
    )
    return AlertPage(items=page.items, next_cursor=page.next_cursor)


@router.post(
    "/admin/alerts/{id}/resolve", status_code=status.HTTP_200_OK, summary="Resolve admin alert"
)
async def resolve_alert(
    id: AdminAlertId,
    body: ResolveAlertBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
) -> Alert:
    row = await session.scalar(
        select(models.AdminAlert)
        .where(models.AdminAlert.id == id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.status == AlertStatus.RESOLVED:
        return serialize_alert(row)
    row.status = AlertStatus.RESOLVED
    row.resolved_at = clock.now()
    row.resolved_by = actor.id
    row.resolution_note = body.note
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.ALERT_RESOLVE,
        entity_type=AuditEntityType.ADMIN_ALERT,
        entity_id=row.id,
        before={"status": "open"},
        after={"status": "resolved"},
        reason=body.reason,
    )
    return serialize_alert(row)
