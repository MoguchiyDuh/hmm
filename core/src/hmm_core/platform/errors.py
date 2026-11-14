from collections.abc import Mapping
from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hmm_contracts.common import ErrorCode, Problem, ValidationIssue
from starlette.exceptions import HTTPException

from hmm_core.platform.config import Config

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
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.INVALID_STATE,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_FILE_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    501: ErrorCode.NOT_IMPLEMENTED,
}


class ProblemError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        errors: list[ValidationIssue] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.errors = errors


def not_implemented() -> NoReturn:
    raise ProblemError(ErrorCode.NOT_IMPLEMENTED)


def problem_response(
    request: Request,
    code: ErrorCode,
    detail: str | None = None,
    errors: list[ValidationIssue] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    config: Config = request.app.state.config
    problem = Problem.build(public_url=config.public_url, code=code, detail=detail, errors=errors)
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=headers,
    )


async def problem_error_handler(request: Request, exc: ProblemError) -> JSONResponse:
    return problem_response(request, exc.code, exc.detail, errors=exc.errors)


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    fallback = ErrorCode.BAD_REQUEST if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR
    return problem_response(
        request,
        HTTP_ERROR_CODES.get(exc.status_code, fallback),
        str(exc.detail),
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return problem_response(
        request,
        ErrorCode.VALIDATION_ERROR,
        errors=[
            ValidationIssue(loc=list(error["loc"]), msg=error["msg"]) for error in exc.errors()
        ],
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(request, ErrorCode.INTERNAL_ERROR)


def register_exception_handlers(app: FastAPI) -> None:
    app.exception_handler(ProblemError)(problem_error_handler)
    app.exception_handler(HTTPException)(http_error_handler)
    app.exception_handler(RequestValidationError)(validation_error_handler)
    app.exception_handler(Exception)(unhandled_error_handler)
