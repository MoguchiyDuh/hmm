import re
from typing import Any
from uuid import UUID

import httpx
import pytest
from hmm_contracts.common import Page, TimeZoneName
from hmm_contracts.notifications import ACTION_ROUTES, ActionKey
from hmm_core.api.schemas.attendance import CreateMemberEventBody, UpdateMemberEventBody
from hmm_core.api.schemas.catalog import EventSummary
from hmm_core.api.security import member, user
from hmm_core.app import create_app
from hmm_core.db.models import User
from pydantic import TypeAdapter, ValidationError


def test_openapi_security_ranges_names_and_project_views() -> None:
    document = create_app().openapi()
    assert Page[EventSummary].__name__ == "EventSummaryPage"
    assert all(not re.search(r"[_-]", name) for name in document["components"]["schemas"])
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in document["components"]["schemas"]
    for path, operations in document["paths"].items():
        for operation in operations.values():
            assert "422" not in operation["responses"]
            if path.startswith("/api/v1/admin/"):
                assert operation["security"] == [{"SessionCookie": []}]
            elif not path.startswith("/api/v1/internal/") and "security" in operation:
                assert operation["security"] == [
                    {"SessionCookie": []},
                    {"BotServiceToken": [], "TelegramUser": []},
                ]
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


@pytest.mark.asyncio
async def test_action_routes_exist_and_fixed_bodies_are_callable(stub_actor: User) -> None:
    app = create_app()
    for dependency in (user, member):
        app.dependency_overrides[dependency] = lambda: stub_actor
    document = app.openapi()
    assert set(ACTION_ROUTES) == set(ActionKey)
    assert len({(route.method, route.path) for route in ACTION_ROUTES.values()}) == len(ActionKey)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for route in ACTION_ROUTES.values():
            operation = document["paths"][route.path][route.method.lower()]
            assert operation["security"] == [
                {"SessionCookie": []},
                {"BotServiceToken": [], "TelegramUser": []},
            ]
            path = route.path.format(id=UUID("019a7b32-0000-7000-8000-000000000001"))
            response = await client.request(route.method, path, json=route.body)
            assert response.status_code == 501, response.text


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
    assert operation["security"] == [
        {"SessionCookie": []},
        {"BotServiceToken": [], "TelegramUser": []},
    ]
