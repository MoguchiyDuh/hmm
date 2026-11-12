import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import hmm_contracts.notifications as notifications
import pytest
from hmm_contracts.common import AlertSeverity, Contract, DeliveryStatus, Locale
from hmm_contracts.notifications import (
    ALERT_SEVERITIES,
    ALLOWED_ACTIONS,
    TELEGRAM_OFF_BY_DEFAULT,
    AlertKind,
    DeliveryAck,
    Notification,
    NotificationActionCallback,
    NotificationPayload,
    NotificationType,
    PendingDeliveriesQuery,
    PendingDelivery,
    TelegramLinkRequest,
    TelegramLinkResult,
    TelegramUnlinkRequest,
)
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts/fixtures/notifications"
PAYLOAD_ADAPTER = TypeAdapter(NotificationPayload)


def fixture_data(notification_type: NotificationType) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{notification_type}.json").read_text())


def table_types(section: str) -> set[str]:
    columns = re.findall(r"^\|\s*(`[^|]+)\|", section, re.MULTILINE)
    return {
        value for column in columns for value in re.findall(r"`([a-z_]+)`", column.split("(", 1)[0])
    }


def test_docs_notification_types() -> None:
    spec = (ROOT / "docs/15-notifications.md").read_text()
    section = spec.split("## Notifications\n", 1)[1].split("## Telegram delivery contract", 1)[0]
    documented = table_types(section)
    assert len(documented) == 74
    assert documented <= set(NotificationType)
    assert set(NotificationType) - documented == {NotificationType.ADMIN_DIGEST}


def test_docs_alert_kinds_and_severities() -> None:
    spec = (ROOT / "docs/15-notifications.md").read_text()
    section = spec.split("## Admin alerts\n", 1)[1].split("## Endpoints", 1)[0]
    assert table_types(section) == set(AlertKind)
    for column, severity in re.findall(
        r"^\|\s*(`[^|]+)\|\s*(info|warning|critical)\s*\|", section, re.MULTILINE
    ):
        for kind in re.findall(r"`([a-z_]+)`", column):
            assert ALERT_SEVERITIES[AlertKind(kind)] == AlertSeverity(severity)


def test_payload_union_and_fixture_coverage() -> None:
    schema = PAYLOAD_ADAPTER.json_schema()
    mapping = schema["discriminator"]["mapping"]
    assert set(mapping) == set(NotificationType)
    assert len(set(mapping.values())) == len(NotificationType)
    assert {path.stem for path in FIXTURES.glob("*.json")} == set(NotificationType)


@pytest.mark.parametrize("notification_type", NotificationType)
def test_fixture_and_discriminator_round_trip(notification_type: NotificationType) -> None:
    data = fixture_data(notification_type)
    notification = Notification.model_validate(data)
    assert notification.type == notification_type
    assert notification.payload.type == notification_type
    assert isinstance(notification.payload, Contract)
    assert notification == Notification.model_validate_json(notification.model_dump_json())
    payload = PAYLOAD_ADAPTER.validate_json(json.dumps(data["payload"]))
    assert payload == notification.payload
    assert PAYLOAD_ADAPTER.validate_json(PAYLOAD_ADAPTER.dump_json(payload)) == payload
    assert {action.action for action in payload.actions} == set(ALLOWED_ACTIONS[notification_type])
    for action in payload.actions:
        callback = NotificationActionCallback(action=action.action, entity_id=action.entity_id)
        assert len(callback.to_callback_data().encode()) <= 64
        assert (
            NotificationActionCallback.from_callback_data(callback.to_callback_data()) == callback
        )


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        PAYLOAD_ADAPTER.validate_python({"type": "unknown_notification", "link": "/notifications"})


def test_confirmed_placement_change_has_no_confirmation_deadline() -> None:
    data = fixture_data(NotificationType.PLACEMENT_CHANGED)
    data["payload"]["state"] = "confirmed"
    data["payload"]["confirm_by"] = None
    data["payload"]["confirm_kind"] = None
    assert Notification.model_validate(data).payload.type == NotificationType.PLACEMENT_CHANGED


@pytest.mark.parametrize(
    "notification_type",
    [
        NotificationType.INVITE_ACCEPTED,
        NotificationType.INVITE_DECLINED,
        NotificationType.INVITE_EXPIRED,
        NotificationType.INVITE_CLOSED,
        NotificationType.JOIN_REQUEST_ACCEPTED,
        NotificationType.JOIN_REQUEST_REJECTED,
    ],
)
def test_standalone_project_outcomes(notification_type: NotificationType) -> None:
    data = fixture_data(notification_type)
    data["payload"]["event_id"] = None
    data["payload"]["event_title"] = None
    if "kind" in data["payload"]:
        data["payload"]["kind"] = "project"
    assert Notification.model_validate(data).payload.type == notification_type


def test_notification_type_mismatch_rejected() -> None:
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["type"] = NotificationType.TEAM_RECONFIRM
    with pytest.raises(ValidationError, match="must match"):
        Notification.model_validate(data)


def test_disallowed_action_rejected() -> None:
    payload = fixture_data(NotificationType.TEAM_PROPOSED)["payload"]
    payload["actions"][0]["action"] = "accept"
    with pytest.raises(ValidationError, match="Action is not allowed"):
        PAYLOAD_ADAPTER.validate_python(payload)


def test_complete_policy_mappings() -> None:
    assert set(ALLOWED_ACTIONS) == set(NotificationType)
    assert set(ALERT_SEVERITIES) == set(AlertKind)
    expected_telegram_off = {
        NotificationType.SCORE_CHANGED,
        NotificationType.TEAM_MEMBER_JOINED,
        NotificationType.PROJECT_MEMBER_JOINED,
        NotificationType.PROJECT_RELEASED,
        NotificationType.ATTENDANCE_CONFIRM_DIGEST,
    }
    assert expected_telegram_off == TELEGRAM_OFF_BY_DEFAULT


def test_all_public_models_exported() -> None:
    for name, value in vars(notifications).items():
        if (
            not name.startswith("_")
            and getattr(value, "__module__", None) == notifications.__name__
        ):
            assert name in notifications.__all__
    assert len(notifications.__all__) == len(set(notifications.__all__))


def test_pending_delivery() -> None:
    data = fixture_data(NotificationType.INVITE_RECEIVED)
    delivery = PendingDelivery.model_validate(
        {
            "delivery_id": "019a7b32-0000-7000-8000-000000000099",
            "notification_id": data["id"],
            "telegram_chat_id": 123456789,
            "locale": "ru",
            "type": data["type"],
            "payload": data["payload"],
            "created_at": data["created_at"],
        }
    )
    assert delivery.locale == Locale.RU
    assert delivery.actions == delivery.payload.actions
    assert PendingDelivery.model_validate_json(delivery.model_dump_json()) == delivery
    mismatched = delivery.model_dump()
    mismatched["type"] = NotificationType.TEAM_PROPOSED
    with pytest.raises(ValidationError, match="must match"):
        PendingDelivery.model_validate(mismatched)


def test_telegram_link_broken_is_web_only() -> None:
    data = fixture_data(NotificationType.TELEGRAM_LINK_BROKEN)
    with pytest.raises(ValidationError, match="web only"):
        PendingDelivery.model_validate(
            {
                "delivery_id": data["id"],
                "notification_id": data["id"],
                "telegram_chat_id": 123456789,
                "locale": "en",
                "type": data["type"],
                "payload": data["payload"],
                "created_at": data["created_at"],
            }
        )


def test_delivery_ack_outcomes() -> None:
    ack = DeliveryAck.model_validate(
        {
            "results": [
                {
                    "delivery_id": "019a7b32-0000-7000-8000-000000000099",
                    "status": status,
                    "external_id": "1042" if status == "sent" else None,
                    "error": None if status == "sent" else "chat not found",
                }
                for status in ("sent", "failed", "permanent_failure")
            ]
        }
    )
    assert ack.results[0].status == DeliveryStatus.SENT
    assert ack.results[1].status == DeliveryStatus.FAILED
    assert ack.results[2].status == "permanent_failure"
    assert DeliveryAck.model_validate_json(ack.model_dump_json()) == ack
    with pytest.raises(ValidationError):
        DeliveryAck.model_validate(
            {"results": [{"delivery_id": ack.results[0].delivery_id, "status": "skipped"}]}
        )


def test_pending_query_defaults_and_limits() -> None:
    query = PendingDeliveriesQuery()
    assert query.limit == 50
    assert query.wait == 25
    for invalid in ({"limit": 0}, {"limit": 101}, {"wait": -1}, {"wait": 26}):
        with pytest.raises(ValidationError):
            PendingDeliveriesQuery.model_validate(invalid)


def test_telegram_link_and_unlink() -> None:
    link = TelegramLinkRequest(
        code="link-code-from-web",
        telegram_user_id=123456789,
        chat_id=123456789,
        username=None,
    )
    result = TelegramLinkResult(
        user_id=UUID("019a7b32-0000-7000-8000-000000000001"), display_name="Alex Morgan"
    )
    unlink = TelegramUnlinkRequest(telegram_user_id=link.telegram_user_id)
    assert TelegramLinkRequest.model_validate_json(link.model_dump_json()) == link
    assert TelegramLinkResult.model_validate_json(result.model_dump_json()) == result
    assert TelegramUnlinkRequest.model_validate_json(unlink.model_dump_json()) == unlink


@pytest.mark.parametrize(
    "data",
    ["confirm", "b:confirm:019a7b32-0000-7000-8000-000000000001", "a:confirm:invalid", "a:" * 40],
)
def test_invalid_callback_data(data: str) -> None:
    with pytest.raises(ValueError):
        NotificationActionCallback.from_callback_data(data)


@pytest.mark.parametrize("link", ["https://example.org", "//example.org", "/bad path"])
def test_non_relative_links_rejected(link: str) -> None:
    data = fixture_data(NotificationType.INVITE_RECEIVED)
    data["payload"]["link"] = link
    with pytest.raises(ValidationError):
        Notification.model_validate(data)


def test_naive_deadlines_and_extra_fields_rejected() -> None:
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["payload"]["confirm_by"] = "2026-11-22T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Notification.model_validate(data)
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["payload"]["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Notification.model_validate(data)
