from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from hmm_contracts.common import (
    AlertSeverity,
    AlertStatus,
    DeliveryStatus,
    ErrorCode,
    Locale,
    NotificationChannel,
)
from hmm_contracts.notifications import (
    ALERT_SEVERITIES,
    TELEGRAM_OFF_BY_DEFAULT,
    AdminAlert,
    AdminAlertPayload,
    AlertKind,
    DeliveryAckResult,
    NotificationPayload,
    NotificationType,
    PendingDelivery,
    TelegramLinkBrokenPayload,
)
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import CursorResult, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.schemas import (
    InboxNotification,
    InboxNotificationPage,
    NotificationAction,
    UnreadCount,
)
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page

LEASE = timedelta(seconds=60)
MAX_ATTEMPTS = 8
BACKOFF_BASE = 30
BACKOFF_CAP = 3600
PENDING_TTL = timedelta(days=7)


def channel_defaults(type: NotificationType) -> tuple[bool, bool]:
    return (
        True,
        type not in TELEGRAM_OFF_BY_DEFAULT and type != NotificationType.TELEGRAM_LINK_BROKEN,
    )


async def channel_settings(
    session: AsyncSession, user_id: UUID
) -> dict[NotificationType, tuple[bool, bool]]:
    settings = {type: channel_defaults(type) for type in NotificationType}
    rows = await session.scalars(
        select(models.NotificationSetting).where(models.NotificationSetting.user_id == user_id)
    )
    for row in rows:
        type = NotificationType(row.type)
        settings[type] = row.web, row.telegram and type != NotificationType.TELEGRAM_LINK_BROKEN
    return settings


async def notify(
    session: AsyncSession,
    user_id: UUID,
    payload: NotificationPayload,
    now: datetime,
    *,
    dedupe_key: str | None = None,
) -> models.Notification | None:
    setting = await session.scalar(
        select(models.NotificationSetting).where(
            models.NotificationSetting.user_id == user_id,
            models.NotificationSetting.type == payload.type.value,
        )
    )
    web, telegram = (
        channel_defaults(payload.type) if setting is None else (setting.web, setting.telegram)
    )
    telegram = telegram and payload.type != NotificationType.TELEGRAM_LINK_BROKEN
    if not web and not telegram:
        return None
    statement = insert(models.Notification).values(
        user_id=user_id,
        type=payload.type.value,
        payload=payload.model_dump(mode="json"),
        dedupe_key=dedupe_key,
        read_at=None if web else now,
    )
    if dedupe_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[models.Notification.dedupe_key]
        )
    row = await session.scalar(statement.returning(models.Notification))
    if row is None:
        return None
    if (
        telegram
        and await session.scalar(
            select(models.TelegramLink.user_id).where(
                models.TelegramLink.user_id == user_id,
                models.TelegramLink.broken_at.is_(None),
            )
        )
        is not None
    ):
        session.add(
            models.NotificationDelivery(
                notification_id=row.id,
                channel=NotificationChannel.TELEGRAM,
                status=DeliveryStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
            )
        )
    await session.flush()
    return row


async def notify_many(
    session: AsyncSession,
    user_ids: Iterable[UUID],
    payload: NotificationPayload,
    now: datetime,
    *,
    dedupe_key: str | None = None,
) -> list[models.Notification]:
    rows: list[models.Notification] = []
    for user_id in dict.fromkeys(user_ids):
        row = await notify(
            session,
            user_id,
            payload,
            now,
            dedupe_key=f"{dedupe_key}:{user_id}" if dedupe_key is not None else None,
        )
        if row is not None:
            rows.append(row)
    return rows


async def raise_alert(
    session: AsyncSession,
    kind: AlertKind,
    now: datetime,
    *,
    title: str,
    message: str,
    link: str,
    subject_type: str,
    subject_id: UUID | None = None,
    subject_title: str = "",
    dedupe_key: str | None = None,
    extra: dict[str, JsonValue] | None = None,
) -> models.AdminAlert | None:
    severity = ALERT_SEVERITIES[kind]
    row = await session.scalar(
        insert(models.AdminAlert)
        .values(
            type=kind.value,
            severity=severity,
            subject_type=subject_type,
            subject_id=subject_id,
            payload={
                "title": title,
                "message": message,
                "link": link,
                "subject_title": subject_title,
                **(extra or {}),
            },
            dedupe_key=dedupe_key,
            status=AlertStatus.OPEN,
        )
        .on_conflict_do_nothing(
            index_elements=[models.AdminAlert.dedupe_key], index_where=text("status = 'open'")
        )
        .returning(models.AdminAlert)
    )
    if row is None:
        return None
    if severity in {AlertSeverity.WARNING, AlertSeverity.CRITICAL}:
        payload = AdminAlertPayload(link=link, alert=alert_contract(row))
        admin_ids = await session.scalars(
            select(models.User.id).where(
                models.User.is_admin.is_(True), models.User.banned_at.is_(None)
            )
        )
        for admin_id in admin_ids:
            await notify(
                session,
                admin_id,
                payload,
                now,
                dedupe_key=f"admin_alert:{row.id}:{admin_id}",
            )
    return row


def alert_contract(row: models.AdminAlert) -> AdminAlert:
    payload = row.payload
    if not isinstance(payload, dict):
        raise ValueError("Admin alert payload must be an object")
    return AdminAlert.model_validate(
        {
            "id": row.id,
            "type": row.type,
            "severity": row.severity,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "subject_title": payload["subject_title"],
            "title": payload["title"],
            "message": payload["message"],
            "link": payload["link"],
            "status": row.status,
            "created_at": row.created_at,
            "resolved_by": row.resolved_by,
            "resolved_at": row.resolved_at,
            "resolution_note": row.resolution_note,
        }
    )


async def resolve_alerts(
    session: AsyncSession,
    dedupe_key: str,
    now: datetime,
    *,
    note: str | None = None,
    actor: UUID | None = None,
) -> int:
    result = await session.execute(
        update(models.AdminAlert)
        .where(
            models.AdminAlert.dedupe_key == dedupe_key, models.AdminAlert.status == AlertStatus.OPEN
        )
        .values(
            status=AlertStatus.RESOLVED,
            resolved_at=now,
            resolved_by=actor,
            resolution_note=note,
        )
    )
    return cast(CursorResult[tuple[()]], result).rowcount


def notification_view(row: models.Notification) -> InboxNotification:
    return InboxNotification(
        id=row.id,
        user_id=row.user_id,
        type=NotificationType(row.type),
        payload=TypeAdapter(NotificationPayload).validate_python(row.payload),
        created_at=row.created_at,
        read_at=row.read_at,
        allowed_actions=[NotificationAction.READ] if row.read_at is None else [],
    )


async def list_notifications(
    session: AsyncSession, user_id: UUID, pagination: CursorParams, unread: bool | None
) -> InboxNotificationPage:
    statement = select(models.Notification).where(models.Notification.user_id == user_id)
    if unread is not None:
        statement = statement.where(
            models.Notification.read_at.is_(None)
            if unread
            else models.Notification.read_at.is_not(None)
        )
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Notification.created_at],
        id_column=models.Notification.id,
        serialize=notification_view,
    )
    return InboxNotificationPage(items=page.items, next_cursor=page.next_cursor)


async def unread_count(session: AsyncSession, user_id: UUID) -> UnreadCount:
    count = await session.scalar(
        select(func.count())
        .select_from(models.Notification)
        .where(models.Notification.user_id == user_id, models.Notification.read_at.is_(None))
    )
    return UnreadCount(count=count or 0)


async def read_notification(
    session: AsyncSession, user_id: UUID, notification_id: UUID, now: datetime
) -> None:
    row = await session.scalar(
        select(models.Notification)
        .where(models.Notification.id == notification_id, models.Notification.user_id == user_id)
        .with_for_update()
    )
    if row is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if row.read_at is None:
        row.read_at = now


async def read_all_notifications(session: AsyncSession, user_id: UUID, now: datetime) -> None:
    await session.execute(
        update(models.Notification)
        .where(models.Notification.user_id == user_id, models.Notification.read_at.is_(None))
        .values(read_at=now)
    )


async def lease_deliveries(
    session: AsyncSession, now: datetime, limit: int
) -> list[PendingDelivery]:
    await session.execute(
        update(models.NotificationDelivery)
        .where(
            models.NotificationDelivery.status == DeliveryStatus.PENDING,
            models.NotificationDelivery.created_at < now - PENDING_TTL,
        )
        .values(status=DeliveryStatus.SKIPPED)
    )
    rows = await session.execute(
        select(models.NotificationDelivery, models.Notification, models.User, models.TelegramLink)
        .join(
            models.Notification,
            models.Notification.id == models.NotificationDelivery.notification_id,
        )
        .join(models.User, models.User.id == models.Notification.user_id)
        .join(models.TelegramLink, models.TelegramLink.user_id == models.Notification.user_id)
        .where(
            models.NotificationDelivery.status == DeliveryStatus.PENDING,
            models.NotificationDelivery.channel == NotificationChannel.TELEGRAM,
            models.NotificationDelivery.next_attempt_at <= now,
            models.TelegramLink.broken_at.is_(None),
            models.User.banned_at.is_(None),
        )
        .order_by(
            models.NotificationDelivery.next_attempt_at,
            models.NotificationDelivery.notification_id,
        )
        .limit(limit)
        .with_for_update(of=models.NotificationDelivery, skip_locked=True)
    )
    deliveries: list[PendingDelivery] = []
    for delivery, notification, user, link in rows:
        delivery.next_attempt_at = now + LEASE
        deliveries.append(
            PendingDelivery(
                delivery_id=notification.id,
                notification_id=notification.id,
                telegram_chat_id=link.chat_id,
                locale=Locale(user.locale),
                type=NotificationType(notification.type),
                payload=TypeAdapter(NotificationPayload).validate_python(notification.payload),
                created_at=notification.created_at,
            )
        )
    await session.flush()
    return deliveries


async def acknowledge(
    session: AsyncSession, now: datetime, results: list[DeliveryAckResult]
) -> None:
    for result in results:
        row = await session.scalar(
            select(models.NotificationDelivery)
            .where(
                models.NotificationDelivery.notification_id == result.delivery_id,
                models.NotificationDelivery.channel == NotificationChannel.TELEGRAM,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.status == DeliveryStatus.SENT:
            continue
        if result.status == DeliveryStatus.SENT:
            row.status = DeliveryStatus.SENT
            row.external_id = result.external_id
            row.last_error = None
            continue
        row.attempts += 1
        row.last_error = result.error
        if result.status == DeliveryStatus.FAILED:
            if row.attempts >= MAX_ATTEMPTS:
                row.status = DeliveryStatus.FAILED
            else:
                row.status = DeliveryStatus.PENDING
                row.next_attempt_at = now + timedelta(
                    seconds=min(BACKOFF_BASE * 2 ** (row.attempts - 1), BACKOFF_CAP)
                )
            continue
        row.status = DeliveryStatus.FAILED
        user_id = await session.scalar(
            select(models.Notification.user_id).where(models.Notification.id == row.notification_id)
        )
        link = await session.scalar(
            select(models.TelegramLink)
            .where(models.TelegramLink.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if link is None or link.broken_at is not None:
            continue
        link.broken_at = now
        await session.execute(
            update(models.NotificationDelivery)
            .where(
                models.NotificationDelivery.notification_id.in_(
                    select(models.Notification.id).where(
                        models.Notification.user_id == link.user_id
                    )
                ),
                models.NotificationDelivery.notification_id != row.notification_id,
                models.NotificationDelivery.status == DeliveryStatus.PENDING,
            )
            .values(status=DeliveryStatus.SKIPPED)
        )
        await notify(
            session,
            link.user_id,
            TelegramLinkBrokenPayload(
                link="/settings",
                broken_at=now,
                reason=result.error or "Telegram chat is unavailable",
            ),
            now,
            dedupe_key=f"telegram_link_broken:{link.user_id}:{now.isoformat()}",
        )
    await session.flush()
