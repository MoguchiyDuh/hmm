import re
import subprocess
import sys
from collections.abc import Iterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from hmm_contracts.common import ErrorCode, Problem
from hmm_core.api.errors import PROBLEM_RESPONSES, ProblemError, not_implemented
from hmm_core.api.pagination import CursorParams
from hmm_core.api.schemas.common import ReasonBody as Reason
from hmm_core.api.security import admin, bot_service, member, parser_service, user
from hmm_core.app import create_app
from hmm_core.config import Config
from hmm_core.db.models import User
from hmm_core.openapi import OPENAPI_PATH, render_openapi
from stub_operations import UNIMPLEMENTED_OPERATIONS

DUMMY_UUID = "019a78e5-9800-7000-8000-000000000001"


def registered_operations(app: FastAPI) -> Iterator[tuple[str, str]]:
    for route in app.routes:
        if isinstance(route, APIRoute):
            if route.include_in_schema:
                for method in sorted(route.methods or set()):
                    yield route.path_format, method.lower()
        elif contexts := getattr(route, "effective_route_contexts", None):
            for context in contexts():
                if isinstance(context.original_route, APIRoute) and context.include_in_schema:
                    for method in sorted(context.methods):
                        yield context.path_format, method.lower()


def minimal_value(schema: dict[str, Any], document: dict[str, Any], depth: int = 0) -> Any:
    if depth > 15:
        return None
    if "$ref" in schema:
        target: Any = document
        for key in schema["$ref"].removeprefix("#/").split("/"):
            target = target[key]
        return minimal_value(target, document, depth + 1)
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return minimal_value(schema[union][0], document, depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            key: minimal_value(value, document, depth + 1)
            for key, value in schema.get("properties", {}).items()
            if key in schema.get("required", [])
        }
    if kind == "array":
        return [
            minimal_value(schema.get("items", {}), document, depth + 1)
            for _ in range(schema.get("minItems", 0))
        ]
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return max(schema.get("minimum", 0), schema.get("exclusiveMinimum", -1) + 1)
    if kind == "null":
        return None
    match schema.get("format"):
        case "uuid":
            return DUMMY_UUID
        case "date-time":
            return "2026-11-12T10:00:00Z"
        case "date":
            return "2026-11-12"
        case "uri" | "url":
            return "https://example.com"
    if schema.get("pattern") == "^[a-z][a-z0-9_]{0,31}$":
        return "backend"
    return "x" * max(1, schema.get("minLength", 0))


def assert_problem(response: httpx.Response, status: int, code: ErrorCode) -> Problem:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    problem = Problem.model_validate(response.json())
    assert problem.status == status
    assert problem.code == code
    assert problem.type.endswith(f"/errors/{code}")
    return problem


@pytest.mark.asyncio
async def test_explicit_unimplemented_operations(stub_actor: User) -> None:
    app = create_app()
    for dependency in (user, member, admin):
        app.dependency_overrides[dependency] = lambda: stub_actor
    document = app.openapi()
    operations = list(registered_operations(app))
    assert operations
    operation_ids = {document["paths"][path][method]["operationId"] for path, method in operations}
    assert operation_ids >= UNIMPLEMENTED_OPERATIONS
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for path, method in operations:
            operation = document["paths"][path][method]
            url = path
            query: dict[str, Any] = {}
            headers: dict[str, str] = {}
            for parameter in operation.get("parameters", []):
                if not parameter.get("required"):
                    continue
                value = minimal_value(parameter["schema"], document)
                name = parameter["name"]
                match parameter["in"]:
                    case "path":
                        url = url.replace("{" + name + "}", str(value))
                    case "query":
                        query[name] = value
                    case "header":
                        headers[name] = str(value)
            url = re.sub(r"\{[^}]+\}", DUMMY_UUID, url)
            body = operation.get("requestBody", {}).get("content", {})
            payload = (
                minimal_value(body["application/json"]["schema"], document)
                if ("application/json" in body)
                else None
            )
            if operation["operationId"] == "catalog_create_event":
                assert isinstance(payload, dict)
                payload["timezone"] = "UTC"
            if operation["operationId"] == "projects_create_release":
                assert isinstance(payload, dict)
                payload["repo_url"] = "https://example.com/repo"
            response = await client.request(
                method, url, params=query, headers=headers, json=payload
            )
            if operation["operationId"] not in UNIMPLEMENTED_OPERATIONS:
                assert response.status_code != 501, operation["operationId"]
                continue
            assert_problem(response, 501, ErrorCode.NOT_IMPLEMENTED)


def test_openapi_is_unique_and_uses_problem_responses() -> None:
    app = create_app()
    operations = list(registered_operations(app))
    assert len(operations) == len(set(operations))
    document = app.openapi()
    ids = []
    for path, method in operations:
        operation = document["paths"][path][method]
        ids.append(operation["operationId"])
        assert re.fullmatch(r"[a-z][a-z0-9_]*", operation["operationId"])
        for status in PROBLEM_RESPONSES:
            content = operation["responses"][str(status)]["content"]
            assert set(content) == {"application/problem+json"}
            assert content["application/problem+json"]["schema"] == {
                "$ref": "#/components/schemas/Problem"
            }
    assert len(ids) == len(set(ids))
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert set(document["components"]["securitySchemes"]) == {
        "SessionCookie",
        "BotServiceToken",
        "ParserServiceToken",
        "TelegramUser",
    }
    assert document["paths"]["/api/v1/areas"]["get"]["security"] == [
        {"SessionCookie": []},
        {"BotServiceToken": [], "TelegramUser": []},
    ]


@pytest.mark.asyncio
async def test_health_probes() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/healthz", "/readyz"):
            response = await client.get(path)
            assert response.status_code == (200 if path == "/healthz" else 503)
            assert response.json() == {"status": "ok" if path == "/healthz" else "unavailable"}
            assert path not in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_validation_and_http_errors() -> None:
    app = create_app(Config(public_url="https://hmm.example/"))

    @app.post("/validation", responses=PROBLEM_RESPONSES)
    async def validation(body: Reason, pagination: Annotated[CursorParams, Depends()]) -> None:
        not_implemented()

    @app.get("/http-error")
    async def http_error() -> None:
        raise HTTPException(401, "Sign in", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/problem-error")
    async def problem_error() -> None:
        raise ProblemError(ErrorCode.INVALID_STATE, "Try again")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("boom")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for path, body in (
            ("/validation?limit=0", {}),
            ("/validation?limit=101", {}),
            ("/validation?limit=invalid", {}),
            ("/validation", {"extra": True}),
        ):
            problem = assert_problem(
                await client.post(path, json=body), 422, ErrorCode.VALIDATION_ERROR
            )
            assert problem.errors
            assert all(issue.loc and issue.msg for issue in problem.errors)
            assert problem.type == "https://hmm.example/errors/validation_error"
        assert_problem(await client.post("/validation", json={}), 501, ErrorCode.NOT_IMPLEMENTED)
        unauthorized = await client.get("/http-error")
        assert_problem(unauthorized, 401, ErrorCode.UNAUTHENTICATED)
        assert unauthorized.headers["WWW-Authenticate"] == "Bearer"
        assert_problem(await client.get("/missing"), 404, ErrorCode.NOT_FOUND)
        conflict = assert_problem(await client.get("/problem-error"), 409, ErrorCode.INVALID_STATE)
        assert conflict.detail == "Try again"
        assert conflict.title == "Invalid state"
        assert_problem(await client.post("/healthz"), 405, ErrorCode.METHOD_NOT_ALLOWED)
        failure = assert_problem(await client.get("/crash"), 500, ErrorCode.INTERNAL_ERROR)
        assert failure.detail is None


@pytest.mark.asyncio
async def test_security_dependencies() -> None:
    app = create_app()

    @app.get("/bot", dependencies=[Depends(bot_service)])
    async def bot() -> None:
        not_implemented()

    @app.get("/parser", dependencies=[Depends(parser_service)])
    async def parser() -> None:
        not_implemented()

    @app.get("/user", dependencies=[Depends(user)])
    async def acting() -> None:
        not_implemented()

    @app.get("/admin", dependencies=[Depends(admin)])
    async def admin_only() -> None:
        not_implemented()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/bot", "/parser"):
            assert_problem(await client.get(path), 501, ErrorCode.NOT_IMPLEMENTED)
    schema = app.openapi()
    assert schema["paths"]["/bot"]["get"]["security"] == [{"BotServiceToken": []}]
    assert schema["paths"]["/parser"]["get"]["security"] == [{"ParserServiceToken": []}]
    assert schema["paths"]["/user"]["get"]["security"] == [
        {"SessionCookie": []},
        {"BotServiceToken": [], "TelegramUser": []},
    ]
    assert schema["paths"]["/admin"]["get"]["security"] == [{"SessionCookie": []}]


def test_openapi_export_is_current() -> None:
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render_openapi()
    result = subprocess.run(
        [sys.executable, "-m", "hmm_core.openapi", "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
