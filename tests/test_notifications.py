import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from hmm.common import AlertSeverity, Contract, DeliveryStatus, Locale
from hmm.notifications.payloads import (
    ACTION_ROUTES,
    ALERT_SEVERITIES,
    ALLOWED_ACTIONS,
    TELEGRAM_OFF_BY_DEFAULT,
    ActionKey,
    AlertKind,
    ApiAction,
    DeliveryAckResult,
    HelpRequestDeclinedPayload,
    HelpRequestDirectPayload,
    HelpRequestExpiredPayload,
    LinkAction,
    Notification,
    NotificationPayload,
    NotificationType,
    PendingDelivery,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/notifications"
PAYLOAD_ADAPTER = TypeAdapter(NotificationPayload)


def fixture_data(notification_type: NotificationType) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{notification_type}.json").read_text())


def table_types(section: str) -> set[str]:
    columns = re.findall(r"^\|\s*(`[^|]+)\|", section, re.MULTILINE)
    return {
        value for column in columns for value in re.findall(r"`([a-z_]+)`", column.split("(", 1)[0])
    }


def test_formation_alert_severities() -> None:
    assert ALERT_SEVERITIES[AlertKind.FORMATION_FAILED] == AlertSeverity.WARNING
    assert ALERT_SEVERITIES[AlertKind.TEAM_BELOW_MIN] == AlertSeverity.INFO


def test_party_invite_expiration() -> None:
    data = fixture_data(NotificationType.PARTY_INVITE)
    payload = data["payload"]
    payload["type"] = data["type"] = NotificationType.INVITE_EXPIRED
    payload["actions"] = []
    notification = Notification.model_validate(data)
    assert notification.payload.type == NotificationType.INVITE_EXPIRED


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
    assert {action.key for action in payload.actions if isinstance(action, ApiAction)} == set(
        ALLOWED_ACTIONS[notification_type]
    )


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        PAYLOAD_ADAPTER.validate_python({"type": "unknown_notification", "link": "/notifications"})


def test_direct_request_decline_and_open_actions() -> None:
    direct = Notification.model_validate(fixture_data(NotificationType.HELP_REQUEST_DIRECT))
    assert isinstance(direct.payload, HelpRequestDirectPayload)
    assert len(direct.payload.actions) == 2
    decline, help_link = direct.payload.actions
    assert isinstance(decline, ApiAction)
    assert decline.key == ActionKey.HELP_REQUEST_DECLINE
    assert decline.entity_id == direct.payload.help_request_id
    assert isinstance(help_link, LinkAction)
    assert help_link.label_key == "notifications.actions.help"
    declined = Notification.model_validate(fixture_data(NotificationType.HELP_REQUEST_DECLINED))
    assert isinstance(declined.payload, HelpRequestDeclinedPayload)
    assert declined.user_id == declined.payload.learner.user_id
    assert len(declined.payload.actions) == 1
    open_action = declined.payload.actions[0]
    assert isinstance(open_action, ApiAction)
    assert open_action.key == ActionKey.HELP_REQUEST_OPEN
    assert open_action.entity_id == declined.payload.help_request_id


def test_expired_request_reopen_links_to_prefilled_new_request() -> None:
    expired = Notification.model_validate(fixture_data(NotificationType.HELP_REQUEST_EXPIRED))
    assert isinstance(expired.payload, HelpRequestExpiredPayload)
    assert len(expired.payload.actions) == 1
    action = expired.payload.actions[0]
    assert isinstance(action, LinkAction)
    assert action.url == f"/mentorship/new?from={expired.payload.help_request_id}"


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
        data["payload"]["kind"] = "team"
    assert Notification.model_validate(data).payload.type == notification_type


def test_notification_type_mismatch_rejected() -> None:
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["type"] = NotificationType.ACCEPT_REMINDER
    with pytest.raises(ValidationError, match="must match"):
        Notification.model_validate(data)


def test_disallowed_action_rejected() -> None:
    payload = fixture_data(NotificationType.TEAM_PROPOSED)["payload"]
    payload["actions"][0]["key"] = "invite_accept"
    with pytest.raises(ValidationError, match="Action is not allowed"):
        PAYLOAD_ADAPTER.validate_python(payload)


def test_complete_policy_mappings() -> None:
    assert set(ACTION_ROUTES) == set(ActionKey)
    assert {key for keys in ALLOWED_ACTIONS.values() for key in keys} == set(ActionKey)
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


def test_action_keys_are_snake_case_and_short() -> None:
    assert all(re.fullmatch(r"[a-z]+(?:_[a-z]+)*", key) for key in ActionKey)
    assert max(map(len, ActionKey)) <= 24


def test_pending_delivery() -> None:
    data = fixture_data(NotificationType.TEAM_INVITE)
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


def test_delivery_ack_result_outcomes() -> None:
    results = [
        DeliveryAckResult(
            delivery_id="019a7b32-0000-7000-8000-000000000099",
            status=status,
            external_id="1042" if status == DeliveryStatus.SENT else None,
            error=None if status == DeliveryStatus.SENT else "chat not found",
        )
        for status in (DeliveryStatus.SENT, DeliveryStatus.FAILED, "permanent_failure")
    ]
    assert results[0].status == DeliveryStatus.SENT
    assert results[1].status == DeliveryStatus.FAILED
    assert results[2].status == "permanent_failure"
    with pytest.raises(ValidationError):
        DeliveryAckResult.model_validate(
            {"delivery_id": results[0].delivery_id, "status": "skipped"}
        )


@pytest.mark.parametrize("link", ["https://example.org", "//example.org", "/bad path"])
def test_non_relative_links_rejected(link: str) -> None:
    data = fixture_data(NotificationType.TEAM_INVITE)
    data["payload"]["link"] = link
    with pytest.raises(ValidationError):
        Notification.model_validate(data)


def test_naive_deadlines_and_extra_fields_rejected() -> None:
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["payload"]["accept_by"] = "2026-11-22T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Notification.model_validate(data)
    data = fixture_data(NotificationType.TEAM_PROPOSED)
    data["payload"]["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Notification.model_validate(data)
