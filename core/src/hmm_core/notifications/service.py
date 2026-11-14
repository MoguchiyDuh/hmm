from collections.abc import Iterable
from datetime import datetime
from typing import cast
from uuid import UUID

from hmm_contracts.common import (
    AlertSeverity,
    AlertStatus,
    DeliveryStatus,
    NotificationChannel,
)
from hmm_contracts.notifications import (
    ALERT_SEVERITIES,
    TELEGRAM_OFF_BY_DEFAULT,
    AdminAlert,
    AdminAlertPayload,
    AlertKind,
    NotificationPayload,
    NotificationType,
)
from pydantic import JsonValue
from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models


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
