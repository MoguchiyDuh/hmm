from datetime import datetime, timedelta

from harness import Harness
from hmm_contracts.common import (
    ErrorCode,
    InviteKind,
    InviteStatus,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
)
from hmm_contracts.notifications import TELEGRAM_OFF_BY_DEFAULT, NotificationType
from hmm_core import models
from hmm_core.identity.service import dnd_active
from sqlalchemy import func, select
from test_api_skeleton import assert_problem


def rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def stored_user(h: Harness, user: models.User) -> models.User:
    async with h.db() as session:
        row = await session.get(models.User, user.id)
        assert row is not None
        return row


async def setting_rows(h: Harness, user: models.User) -> dict[str, tuple[bool, bool]]:
    async with h.db() as session:
        rows = await session.scalars(
            select(models.NotificationSetting).where(models.NotificationSetting.user_id == user.id)
        )
        return {row.type: (row.web, row.telegram) for row in rows}


def default_items() -> list[dict[str, object]]:
    return [
        {
            "type": type.value,
            "web": True,
            "telegram": type not in TELEGRAM_OFF_BY_DEFAULT
            and type != NotificationType.TELEGRAM_LINK_BROKEN,
        }
        for type in NotificationType
    ]


async def test_availability_indefinite_timed_expiry_and_clear(harness: Harness) -> None:
    h = harness
    member = await h.user()
    until = h.now + timedelta(days=1)
    async with await h.client(member) as c:
        response = await c.put("/api/v1/me/availability", json={"dnd_until": "indefinite"})
        assert response.status_code == 200, response.text
        assert response.json() == {"dnd_until": "indefinite", "allowed_actions": ["edit"]}
        row = await stored_user(h, member)
        assert row.dnd_indefinite is True
        assert row.dnd_until is None
        response = await c.get("/api/v1/me")
        assert response.status_code == 200, response.text
        assert response.json()["dnd_until"] == "indefinite"

        response = await c.put("/api/v1/me/availability", json={"dnd_until": rfc3339(until)})
        assert response.status_code == 200, response.text
        assert datetime.fromisoformat(response.json()["dnd_until"]) == until
        assert response.json()["allowed_actions"] == ["edit"]
        row = await stored_user(h, member)
        assert row.dnd_indefinite is False
        assert row.dnd_until == until
        assert dnd_active(row, h.now) is True

        h.advance(timedelta(days=2))
        row = await stored_user(h, member)
        assert dnd_active(row, h.now) is False
        assert row.dnd_until == until

        response = await c.put("/api/v1/me/availability", json={"dnd_until": None})
        assert response.status_code == 200, response.text
        assert response.json() == {"dnd_until": None, "allowed_actions": ["edit"]}
        row = await stored_user(h, member)
        assert row.dnd_indefinite is False
        assert row.dnd_until is None
        assert dnd_active(row, h.now) is False

        response = await c.put("/api/v1/me/availability", json={"dnd_until": None})
        assert response.status_code == 200, response.text
        assert response.json()["dnd_until"] is None
        assert (await stored_user(h, member)).updated_at == row.updated_at


async def test_availability_access_and_validation(harness: Harness) -> None:
    h = harness
    member = await h.user()
    pending = await h.user(onboarded=False)
    async with await h.client(pending) as c:
        response = await c.put("/api/v1/me/availability", json={"dnd_until": "indefinite"})
    assert_problem(response, 403, ErrorCode.ONBOARDING_REQUIRED)
    async with h.anonymous() as c:
        response = await c.put("/api/v1/me/availability", json={"dnd_until": "indefinite"})
    assert_problem(response, 401, ErrorCode.UNAUTHENTICATED)
    async with await h.client(member) as c:
        response = await c.put("/api/v1/me/availability", json={"dnd_until": "tomorrow"})
    assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    row = await stored_user(h, member)
    assert row.dnd_indefinite is False
    assert row.dnd_until is None


async def test_availability_leaves_pending_invites_untouched(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with h.db() as session:
        project = models.Project(
            name="P",
            description_md="",
            goal="g",
            links=[],
            visibility=ProjectVisibility.MEMBERS.value,
            status=ProjectStatus.ACTIVE.value,
            origin=ProjectOrigin.STANDALONE.value,
            lead_user_id=member.id,
        )
        session.add(project)
        await session.flush()
        invite = models.Invite(
            project_id=project.id,
            user_id=member.id,
            kind=InviteKind.FILL.value,
            status=InviteStatus.PENDING.value,
            expires_at=h.now + timedelta(days=1),
        )
        session.add(invite)
        await session.flush()
        await session.refresh(invite)
        await session.refresh(project)
    async with await h.client(member) as c:
        response = await c.put("/api/v1/me/availability", json={"dnd_until": "indefinite"})
    assert response.status_code == 200, response.text
    async with h.db() as session:
        stored_invite = await session.get(models.Invite, invite.id)
        stored_project = await session.get(models.Project, project.id)
        assert stored_invite is not None
        assert stored_project is not None
        assert stored_invite.status == InviteStatus.PENDING.value
        assert stored_invite.responded_at is None
        assert stored_invite.closed_reason is None
        assert stored_invite.updated_at == invite.updated_at
        assert stored_project.updated_at == project.updated_at
    assert await h.pending_jobs() == []


async def test_notification_settings_defaults(harness: Harness) -> None:
    h = harness
    member = await h.user()
    async with await h.client(member) as c:
        response = await c.get("/api/v1/me/notification-settings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed_actions"] == ["edit"]
    items = body["items"]
    assert len(items) == len(NotificationType)
    assert [item["type"] for item in items] == [type.value for type in NotificationType]
    assert all(item["web"] for item in items)
    by_type = {item["type"]: item for item in items}
    for type in TELEGRAM_OFF_BY_DEFAULT:
        assert by_type[type.value]["telegram"] is False
    assert len(TELEGRAM_OFF_BY_DEFAULT) == 5
    assert by_type[NotificationType.TELEGRAM_LINK_BROKEN.value]["telegram"] is False
    assert by_type[NotificationType.INVITE_RECEIVED.value]["telegram"] is True
    assert items == default_items()


async def test_notification_settings_partial_update_and_overrides(harness: Harness) -> None:
    h = harness
    member = await h.user()
    body = {
        "items": [
            {"type": "score_changed", "web": False, "telegram": True},
            {"type": "invite_received", "web": True, "telegram": False},
        ]
    }
    async with await h.client(member) as c:
        response = await c.put("/api/v1/me/notification-settings", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["allowed_actions"] == ["edit"]
    expected = default_items()
    for item in expected:
        if item["type"] == "score_changed":
            item.update(web=False, telegram=True)
        if item["type"] == "invite_received":
            item.update(telegram=False)
    assert response.json()["items"] == expected
    assert await setting_rows(h, member) == {
        "score_changed": (False, True),
        "invite_received": (True, False),
    }


async def test_notification_settings_telegram_link_broken_is_web_only(harness: Harness) -> None:
    h = harness
    member = await h.user()
    body = {"items": [{"type": "telegram_link_broken", "web": True, "telegram": True}]}
    async with await h.client(member) as c:
        response = await c.put("/api/v1/me/notification-settings", json=body)
    assert response.status_code == 200, response.text
    by_type = {item["type"]: item for item in response.json()["items"]}
    assert by_type["telegram_link_broken"] == {
        "type": "telegram_link_broken",
        "web": True,
        "telegram": False,
    }
    assert await setting_rows(h, member) == {"telegram_link_broken": (True, False)}


async def test_notification_settings_are_per_actor(harness: Harness) -> None:
    h = harness
    first = await h.user()
    second = await h.user()
    body = {"items": [{"type": "invite_received", "web": False, "telegram": False}]}
    async with await h.client(first) as c:
        response = await c.put("/api/v1/me/notification-settings", json=body)
    assert response.status_code == 200, response.text
    async with await h.client(second) as c:
        response = await c.get("/api/v1/me/notification-settings")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == default_items()
    assert await setting_rows(h, second) == {}
    assert await setting_rows(h, first) == {"invite_received": (False, False)}


async def test_notification_settings_available_before_onboarding(harness: Harness) -> None:
    h = harness
    pending = await h.user(onboarded=False)
    body = {"items": [{"type": "invite_received", "web": False, "telegram": True}]}
    async with await h.client(pending) as c:
        response = await c.get("/api/v1/me/notification-settings")
        assert response.status_code == 200, response.text
        response = await c.put("/api/v1/me/notification-settings", json=body)
    assert response.status_code == 200, response.text
    assert await setting_rows(h, pending) == {"invite_received": (False, True)}


async def test_notification_settings_update_is_idempotent_and_side_effect_free(
    harness: Harness,
) -> None:
    h = harness
    member = await h.user()
    body = {
        "items": [
            {"type": "score_changed", "web": False, "telegram": True},
            {"type": "invite_received", "web": True, "telegram": True},
            {"type": "invite_received", "web": False, "telegram": False},
        ]
    }
    async with await h.client(member) as c:
        first = await c.put("/api/v1/me/notification-settings", json=body)
        second = await c.put("/api/v1/me/notification-settings", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert await setting_rows(h, member) == {
        "score_changed": (False, True),
        "invite_received": (False, False),
    }
    assert await h.pending_jobs() == []
    async with h.db() as session:
        assert await session.scalar(select(func.count()).select_from(models.Notification)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0
