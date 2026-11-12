from importlib.metadata import version
from typing import Any

from fastapi import FastAPI

from hmm_core.api.errors import register_exception_handlers
from hmm_core.api.openapi import build_openapi, operation_id
from hmm_core.api.routers import ROUTERS
from hmm_core.config import Config


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def readyz() -> dict[str, str]:
    return {"status": "ok"}


def create_app(config: Config | None = None) -> FastAPI:
    app = FastAPI(
        title="hmm",
        version=version("hmm-core"),
        generate_unique_id_function=operation_id,
        separate_input_output_schemas=False,
    )
    app.state.config = config if config is not None else Config.from_env()
    register_exception_handlers(app)
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    for router, internal in ROUTERS:
        app.include_router(router, prefix="/api/v1")
        app.include_router(internal, prefix="/api/v1/internal")

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi
    return app
