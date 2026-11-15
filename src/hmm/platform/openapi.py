import re
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute


def operation_id(route: APIRoute) -> str:
    tag = str(route.tags[0]) if route.tags else "system"
    return re.sub(r"[^a-z0-9_]+", "_", f"{tag}_{route.name}".lower()).strip("_")


def drop_plain_json_errors(operation: dict[str, Any]) -> None:
    for response in operation.get("responses", {}).values():
        content = response.get("content", {})
        if "application/problem+json" in content:
            content.pop("application/json", None)


def build_openapi(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    for path in schema["paths"].values():
        for operation in path.values():
            drop_plain_json_errors(operation)
    return schema
