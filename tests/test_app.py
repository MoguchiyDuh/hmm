import re
from collections.abc import AsyncIterator, Iterator
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from harness import assert_problem

from hmm.app import create_app
from hmm.common import ErrorCode
from hmm.platform.config import Config
from hmm.platform.db import get_session
from hmm.platform.errors import PROBLEM_RESPONSES, ProblemError
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import ReasonBody as Reason
from hmm.platform.security import admin, user

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
    assert set(document["components"]["securitySchemes"]) == {"SessionCookie"}
    assert document["paths"]["/api/v1/areas"]["get"]["security"] == [{"SessionCookie": []}]


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
        return None

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

    async def no_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_session] = no_session

    @app.get("/user", dependencies=[Depends(user)])
    async def acting() -> None:
        return None

    @app.get("/admin", dependencies=[Depends(admin)])
    async def admin_only() -> None:
        return None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/user", "/admin"):
            assert_problem(await client.get(path), 401, ErrorCode.UNAUTHENTICATED)
    schema = app.openapi()
    for path in ("/user", "/admin"):
        assert schema["paths"][path]["get"]["security"] == [{"SessionCookie": []}]
