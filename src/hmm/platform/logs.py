import json
import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log_fields: ContextVar[Mapping[str, str | int]] = ContextVar(
    "log_fields", default=MappingProxyType({})
)

REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **log_fields.get(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if REQUEST_ID.fullmatch(incoming) else uuid4().hex
        header = (b"x-request-id", request_id.encode("latin-1"))

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), header]
            await send(message)

        token = log_fields.set({"request_id": request_id})
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log_fields.reset(token)
