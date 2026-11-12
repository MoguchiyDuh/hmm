import re
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from hmm_core.api.errors import register_exception_handlers
from hmm_core.api.routers import ROUTERS
from hmm_core.api.security import SECURITY_SCHEMES


def operation_id(route: APIRoute) -> str:
    tag = str(route.tags[0]) if route.tags else "system"
    return re.sub(r"[^a-z0-9_]+", "_", f"{tag}_{route.name}".lower()).strip("_")


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def readyz() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(
        title="hmm",
        version=version("hmm-core"),
        generate_unique_id_function=operation_id,
        separate_input_output_schemas=False,
    )
    register_exception_handlers(app)
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    for router, internal in ROUTERS:
        app.include_router(router, prefix="/api/v1")
        app.include_router(internal, prefix="/api/v1/internal")

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
                separate_input_output_schemas=app.separate_input_output_schemas,
            )
            schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(
                SECURITY_SCHEMES
            )
            for path in schema["paths"].values():
                for operation in path.values():
                    for response in operation.get("responses", {}).values():
                        content = response.get("content", {})
                        if "application/problem+json" in content:
                            content.pop("application/json", None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app
