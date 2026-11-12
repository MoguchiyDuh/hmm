import re
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from hmm_core.api.security import SECURITY_SCHEMES

USER_SECURITY = [{"SessionCookie": []}, {"BotServiceToken": [], "TelegramUser": []}]


def operation_id(route: APIRoute) -> str:
    tag = str(route.tags[0]) if route.tags else "system"
    return re.sub(r"[^a-z0-9_]+", "_", f"{tag}_{route.name}".lower()).strip("_")


def normalize_security(operation: dict[str, Any]) -> None:
    if {"TelegramUser": []} in operation.get("security", []):
        operation["security"] = USER_SECURITY


def drop_plain_json_errors(operation: dict[str, Any]) -> None:
    for response in operation.get("responses", {}).values():
        content = response.get("content", {})
        if "application/problem+json" in content:
            content.pop("application/json", None)


def normalize_schema_names(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.get("components", {}).get("schemas", {})
    names = {
        name: definition["title"]
        for name, definition in components.items()
        if re.search(r"[_-]", name)
    }
    renamed = [names.get(name, name) for name in components]
    if len(renamed) != len(set(renamed)) or any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name) for name in renamed
    ):
        raise ValueError("OpenAPI schema names must be unique alphanumeric names")
    references = {
        f"#/components/schemas/{old}": f"#/components/schemas/{new}" for old, new in names.items()
    }

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str):
            return references.get(value, value)
        return value

    schema["components"]["schemas"] = {
        names.get(name, name): definition for name, definition in components.items()
    }
    return rewrite(schema)


def build_openapi(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        separate_input_output_schemas=app.separate_input_output_schemas,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(SECURITY_SCHEMES)
    for path in schema["paths"].values():
        for operation in path.values():
            normalize_security(operation)
            drop_plain_json_errors(operation)
    return normalize_schema_names(schema)
