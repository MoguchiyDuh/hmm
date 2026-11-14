from datetime import timedelta

import pytest
from harness import Harness
from hmm_contracts.common import AlertSeverity, AlertStatus, DeliveryStatus, NotificationChannel
from hmm_contracts.fixtures import load_notifications
from hmm_contracts.notifications import (
    TELEGRAM_OFF_BY_DEFAULT,
    AdminAlertPayload,
    AlertKind,
    NotificationPayload,
    NotificationType,
)
from hmm_core import models
from hmm_core.notifications.service import (
    alert_contract,
    channel_defaults,
    channel_settings,
    notify,
    notify_many,
    raise_alert,
    resolve_alerts,
)
from pydantic import TypeAdapter
from sqlalchemy import func, select

NOTIFICATIONS = load_notifications()
PAYLOAD = NOTIFICATIONS["invite_received"].payload


def test_channel_defaults() -> None:
    for type in NotificationType:
        telegram = (
            type not in TELEGRAM_OFF_BY_DEFAULT and type != NotificationType.TELEGRAM_LINK_BROKEN
        )
        assert channel_defaults(type) == (True, telegram)


async def test_channel_settings(harness: Harness) -> None:
    h = harness
    user = await h.user()
    other = await h.user()
    async with h.db() as session:
        assert await channel_settings(session, user.id) == {
            type: channel_defaults(type) for type in NotificationType
        }
        session.add_all(
            [
                models.NotificationSetting(
                    user_id=user.id,
                    type=NotificationType.INVITE_RECEIVED.value,
                    web=False,
                    telegram=True,
                ),
                models.NotificationSetting(
                    user_id=user.id,
                    type=NotificationType.SCORE_CHANGED.value,
                    web=True,
                    telegram=True,
                ),
                models.NotificationSetting(
                    user_id=user.id,
                    type=NotificationType.TELEGRAM_LINK_BROKEN.value,
                    web=True,
                    telegram=True,
                ),
                models.NotificationSetting(
                    user_id=other.id,
                    type=NotificationType.INVITE_RECEIVED.value,
                    web=True,
                    telegram=False,
                ),
            ]
        )
        settings = await channel_settings(session, user.id)
        assert set(settings) == set(NotificationType)
        assert settings[NotificationType.INVITE_RECEIVED] == (False, True)
        assert settings[NotificationType.SCORE_CHANGED] == (True, True)
        assert settings[NotificationType.TELEGRAM_LINK_BROKEN] == (True, False)
        for type in NotificationType:
            if type not in {
                NotificationType.INVITE_RECEIVED,
                NotificationType.SCORE_CHANGED,
                NotificationType.TELEGRAM_LINK_BROKEN,
            }:
                assert settings[type] == channel_defaults(type)


@pytest.mark.parametrize(
    "payload", [notification.payload for notification in NOTIFICATIONS.values()], ids=NOTIFICATIONS
)
async def test_notify_fixture(harness: Harness, payload: NotificationPayload) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    async with h.db() as session:
        row = await notify(session, user.id, payload, h.now)
        assert row is not None
        assert row.user_id == user.id
        assert row.type == payload.type.value
        assert row.payload == payload.model_dump(mode="json")
        assert row.dedupe_key is None
        assert row.read_at is None
        assert row.created_at is not None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        delivery = await session.scalar(select(models.NotificationDelivery))
        if channel_defaults(payload.type)[1]:
            assert delivery is not None
            assert delivery.notification_id == row.id
            assert delivery.channel == NotificationChannel.TELEGRAM
            assert delivery.status == DeliveryStatus.PENDING
            assert delivery.attempts == 0
            assert delivery.next_attempt_at == h.now
        else:
            assert delivery is None
    async with h.db() as session:
        stored = await session.get(models.Notification, row.id)
        assert stored is not None
        assert stored.payload == payload.model_dump(mode="json")


@pytest.mark.parametrize("broken", [False, True])
async def test_notify_without_active_telegram_link(harness: Harness, broken: bool) -> None:
    h = harness
    user = await h.user()
    if broken:
        await h.link_telegram(user)
    async with h.db() as session:
        if broken:
            link = await session.get(models.TelegramLink, user.id)
            assert link is not None
            link.broken_at = h.now
        row = await notify(session, user.id, PAYLOAD, h.now)
        assert row is not None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert await session.scalar(select(models.NotificationDelivery)) is None


@pytest.mark.parametrize("telegram", [False, True])
async def test_notify_with_web_disabled(harness: Harness, telegram: bool) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=user.id, type=PAYLOAD.type.value, web=False, telegram=telegram
            )
        )
        row = await notify(session, user.id, PAYLOAD, h.now)
        delivery = await session.scalar(select(models.NotificationDelivery))
        if telegram:
            assert row is not None
            assert row.read_at == h.now
            assert delivery is not None
            assert delivery.notification_id == row.id
            assert delivery.status == DeliveryStatus.PENDING
        else:
            assert row is None
            assert delivery is None
            assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0


async def test_notify_settings_enable_default_disabled_telegram(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    payload = NOTIFICATIONS["score_changed"].payload
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=user.id, type=payload.type.value, web=True, telegram=True
            )
        )
        row = await notify(session, user.id, payload, h.now)
        assert row is not None
        assert (
            await session.get(models.NotificationDelivery, (row.id, NotificationChannel.TELEGRAM))
            is not None
        )


async def test_notify_telegram_link_broken_is_always_web_only(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    payload = NOTIFICATIONS["telegram_link_broken"].payload
    async with h.db() as session:
        setting = models.NotificationSetting(
            user_id=user.id, type=payload.type.value, web=True, telegram=True
        )
        session.add(setting)
        assert await notify(session, user.id, payload, h.now) is not None
        setting.web = False
        assert await notify(session, user.id, payload, h.now) is None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert await session.scalar(select(models.NotificationDelivery)) is None


async def test_notify_dedupe(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    async with h.db() as session:
        row = await notify(session, user.id, PAYLOAD, h.now, dedupe_key="notification:one")
        assert row is not None
        assert row.dedupe_key == "notification:one"
    async with h.db() as session:
        assert await notify(session, user.id, PAYLOAD, h.now, dedupe_key="notification:one") is None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 1
        )


async def test_notify_rollback(harness: Harness) -> None:
    h = harness
    user = await h.user()
    await h.link_telegram(user)
    async with h.sessionmaker() as session:
        row = await notify(session, user.id, PAYLOAD, h.now, dedupe_key="rollback")
        assert row is not None
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 1
        )
        await session.rollback()
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 0
        )


@pytest.mark.parametrize("dedupe_key", [None, "broadcast"])
async def test_notify_many(harness: Harness, dedupe_key: str | None) -> None:
    h = harness
    first = await h.user()
    second = await h.user()
    disabled = await h.user()
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=disabled.id, type=PAYLOAD.type.value, web=False, telegram=False
            )
        )
        rows = await notify_many(
            session,
            iter([second.id, first.id, second.id, disabled.id, first.id]),
            PAYLOAD,
            h.now,
            dedupe_key=dedupe_key,
        )
        assert [row.user_id for row in rows] == [second.id, first.id]
        assert [row.dedupe_key for row in rows] == [
            f"{dedupe_key}:{user_id}" if dedupe_key is not None else None
            for user_id in [second.id, first.id]
        ]
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2
        if dedupe_key is not None:
            assert (
                await notify_many(
                    session, [first.id, second.id], PAYLOAD, h.now, dedupe_key=dedupe_key
                )
                == []
            )


@pytest.mark.parametrize("kind", [AlertKind.TEAM_BELOW_MIN, AlertKind.ALGO_FAILURE])
async def test_raise_alert(harness: Harness, kind: AlertKind) -> None:
    h = harness
    admin = await h.admin()
    await h.admin(banned_at=h.now)
    member = await h.user()
    await h.link_telegram(admin)
    async with h.db() as session:
        row = await raise_alert(
            session,
            kind,
            h.now,
            title="Attention needed",
            message="A condition needs attention",
            link="/admin",
            subject_type="user",
            subject_id=member.id,
            subject_title=member.display_name,
            dedupe_key="condition:one",
            extra={"count": 2},
        )
        assert row is not None
        assert row.type == kind.value
        assert row.severity == (
            AlertSeverity.WARNING if kind == AlertKind.TEAM_BELOW_MIN else AlertSeverity.CRITICAL
        )
        assert row.status == AlertStatus.OPEN
        assert row.payload == {
            "title": "Attention needed",
            "message": "A condition needs attention",
            "link": "/admin",
            "subject_title": member.display_name,
            "count": 2,
        }
        contract = alert_contract(row)
        assert contract.id == row.id
        assert contract.type == kind
        assert contract.subject_type == "user"
        assert contract.subject_id == member.id
        assert contract.subject_title == member.display_name
        assert contract.title == "Attention needed"
        assert contract.message == "A condition needs attention"
        assert contract.link == "/admin"
        assert contract.created_at == row.created_at
        notifications = list(await session.scalars(select(models.Notification)))
        assert len(notifications) == 1
        notification = notifications[0]
        assert notification.user_id == admin.id
        assert notification.type == NotificationType.ADMIN_ALERT
        assert notification.dedupe_key == f"admin_alert:{row.id}:{admin.id}"
        payload = TypeAdapter(NotificationPayload).validate_python(notification.payload)
        assert isinstance(payload, AdminAlertPayload)
        assert payload.alert == contract
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 1
        )
    async with h.db() as session:
        assert (
            await raise_alert(
                session,
                kind,
                h.now,
                title="Duplicate",
                message="Duplicate condition",
                link="/admin",
                subject_type="user",
                dedupe_key="condition:one",
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 1
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 1
        )
        assert await resolve_alerts(session, "condition:one", h.now) == 1
        replacement = await raise_alert(
            session,
            kind,
            h.now,
            title="Reappeared",
            message="Condition returned",
            link="/admin",
            subject_type="user",
            dedupe_key="condition:one",
        )
        assert replacement is not None
        assert replacement.id != row.id
        assert replacement.status == AlertStatus.OPEN
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 2
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 2


async def test_raise_info_alert_without_notifications(harness: Harness) -> None:
    h = harness
    await h.admin()
    async with h.db() as session:
        for _ in range(2):
            row = await raise_alert(
                session,
                AlertKind.FORMATION_UNDERSIZED,
                h.now,
                title="Formation undersized",
                message="Not enough members",
                link="/admin",
                subject_type="event",
            )
            assert row is not None
            assert row.severity == AlertSeverity.INFO
            assert row.subject_id is None
            assert row.dedupe_key is None
            assert alert_contract(row).subject_title == ""
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 2
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 0
        )


async def test_raise_alert_respects_admin_toggles(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    async with h.db() as session:
        session.add(
            models.NotificationSetting(
                user_id=admin.id, type=NotificationType.ADMIN_ALERT.value, web=False, telegram=False
            )
        )
        assert (
            await raise_alert(
                session,
                AlertKind.TEAM_BELOW_MIN,
                h.now,
                title="Team below minimum",
                message="Too few members",
                link="/admin",
                subject_type="project",
            )
            is not None
        )
        assert await session.scalar(select(func.count()).select_from(models.AdminAlert)) == 1
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0


async def test_resolve_alerts(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with h.db() as session:
        rows: list[models.AdminAlert] = []
        for key in ["condition:one", "condition:other"]:
            row = await raise_alert(
                session,
                AlertKind.FORMATION_UNDERSIZED,
                h.now,
                title="Formation undersized",
                message="Not enough members",
                link="/admin",
                subject_type="event",
                dedupe_key=key,
            )
            assert row is not None
            rows.append(row)
    now = h.advance(timedelta(minutes=5))
    async with h.db() as session:
        assert (
            await resolve_alerts(
                session, "condition:one", now, note="Condition cleared", actor=actor.id
            )
            == 1
        )
        assert (
            await resolve_alerts(
                session, "condition:one", now + timedelta(minutes=1), note="Ignored"
            )
            == 0
        )
        assert await resolve_alerts(session, "missing", now) == 0
    async with h.db() as session:
        resolved = await session.get(models.AdminAlert, rows[0].id)
        assert resolved is not None
        assert resolved.status == AlertStatus.RESOLVED
        assert resolved.resolved_at == now
        assert resolved.resolved_by == actor.id
        assert resolved.resolution_note == "Condition cleared"
        contract = alert_contract(resolved)
        assert contract.status == AlertStatus.RESOLVED
        assert contract.resolved_at == now
        assert contract.resolved_by == actor.id
        assert contract.resolution_note == "Condition cleared"
        untouched = await session.get(models.AdminAlert, rows[1].id)
        assert untouched is not None
        assert untouched.status == AlertStatus.OPEN
        assert untouched.resolved_at is None
        assert untouched.resolved_by is None
        assert untouched.resolution_note is None
