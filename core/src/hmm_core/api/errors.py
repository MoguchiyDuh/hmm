from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hmm_contracts.common import ErrorCode, Problem, ValidationIssue
from starlette.exceptions import HTTPException

from hmm_core.config import public_url

PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {
        "model": Problem,
        "description": description,
        "content": {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
        },
    }
    for status, description in (("4XX", "Client error"), ("5XX", "Server error"))
}

HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.BAD_REQUEST,
    409: ErrorCode.INVALID_STATE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    501: ErrorCode.NOT_IMPLEMENTED,
}


class ProblemError(Exception):
    def __init__(self, status: int, code: ErrorCode, title: str, detail: str | None = None) -> None:
        super().__init__(title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def not_implemented() -> NoReturn:
    raise ProblemError(501, ErrorCode.NOT_IMPLEMENTED, "Not implemented")


def problem_response(problem: Problem, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=headers,
    )


async def problem_error_handler(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(
        Problem.build(
            public_url=public_url(),
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
        )
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    try:
        title = HTTPStatus(exc.status_code).phrase
    except ValueError:
        title = "HTTP error"
    default_code = ErrorCode.BAD_REQUEST if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR
    return problem_response(
        Problem.build(
            public_url=public_url(),
            status=exc.status_code,
            code=HTTP_ERROR_CODES.get(exc.status_code, default_code),
            title=title,
            detail=str(exc.detail),
        ),
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return problem_response(
        Problem.build(
            public_url=public_url(),
            status=422,
            code=ErrorCode.VALIDATION_ERROR,
            title="Validation error",
            errors=[
                ValidationIssue(loc=list(error["loc"]), msg=error["msg"]) for error in exc.errors()
            ],
        )
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.exception_handler(ProblemError)(problem_error_handler)
    app.exception_handler(HTTPException)(http_error_handler)
    app.exception_handler(RequestValidationError)(validation_error_handler)
