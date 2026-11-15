import re
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from hmm.app import create_app
from hmm.attendance.schemas import CreateMemberEventBody, UpdateMemberEventBody
from hmm.catalog.schemas import EventSummary
from hmm.common import Page, TimeZoneName
from hmm.notifications.payloads import ACTION_ROUTES, ActionKey


def test_openapi_security_ranges_names_and_project_views() -> None:
    document = create_app().openapi()
    assert Page[EventSummary].__name__ == "EventSummaryPage"
    assert all(not re.search(r"[_-]", name) for name in document["components"]["schemas"])
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in document["components"]["schemas"]
    for operations in document["paths"].values():
        for operation in operations.values():
            assert "422" not in operation["responses"]
            if "security" in operation:
                assert operation["security"] == [{"SessionCookie": []}]
    response = document["paths"]["/api/v1/projects/{id}"]["get"]["responses"]["200"]
    schema = response["content"]["application/json"]["schema"]
    schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert schema["discriminator"]["propertyName"] == "view"
    assert set(schema["discriminator"]["mapping"]) == {"full", "limited"}
    response = document["paths"]["/api/v1/projects"]["get"]["responses"]["200"]
    name = response["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1]
    items = document["components"]["schemas"][name]["properties"]["items"]["items"]
    items = document["components"]["schemas"][items["$ref"].rsplit("/", 1)[1]]
    assert items["discriminator"]["propertyName"] == "view"


def test_action_routes_exist() -> None:
    document = create_app().openapi()
    assert set(ACTION_ROUTES) == set(ActionKey)
    assert len({(route.method, route.path) for route in ACTION_ROUTES.values()}) == len(ActionKey)
    for route in ACTION_ROUTES.values():
        operation = document["paths"][route.path][route.method.lower()]
        assert operation["security"] == [{"SessionCookie": []}]


def test_timezone_type_and_member_event_defaults() -> None:
    adapter = TypeAdapter(TimeZoneName)
    assert adapter.json_schema()["type"] == "string"
    assert adapter.validate_python("Europe/Berlin") == "Europe/Berlin"
    for value in ("", "Not/AZone", "/etc/passwd", "../UTC"):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
    body: dict[str, Any] = {
        "kind": "meetup",
        "title": "Meeting",
        "format": "online",
        "starts_at": "2026-11-12T10:00:00Z",
        "ends_at": "2026-11-12T11:00:00Z",
    }
    assert CreateMemberEventBody.model_validate(body).timezone == "UTC"
    assert UpdateMemberEventBody(timezone="Asia/Tokyo").timezone == "Asia/Tokyo"


def test_direct_help_request_decline_has_no_body_and_returns_help_request() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/help-requests/{id}/decline"]["post"]
    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HelpRequest"
    }
    assert operation["security"] == [{"SessionCookie": []}]
