from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from hmm_core.api.errors import register_exception_handlers
from hmm_core.api.openapi import build_openapi, operation_id
from hmm_core.api.routers import ROUTERS
from hmm_core.clock import SystemClock
from hmm_core.config import Config
from hmm_core.db import create_engine, create_sessionmaker, migration_head


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def readyz(request: Request) -> JSONResponse:
    engine: AsyncEngine | None = request.app.state.engine
    if engine is not None:
        try:
            async with engine.connect() as connection:
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if version == migration_head():
                return JSONResponse({"status": "ok"})
        except (SQLAlchemyError, OSError):
            pass
    return JSONResponse({"status": "unavailable"}, status_code=503)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    config: Config = app.state.config
    engine = create_engine(config.database_url) if config.database_url else None
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine) if engine is not None else None
    try:
        yield
    finally:
        if engine is not None:
            await engine.dispose()


def create_app(config: Config | None = None) -> FastAPI:
    app = FastAPI(
        title="hmm",
        version=version("hmm-core"),
        generate_unique_id_function=operation_id,
        separate_input_output_schemas=False,
        lifespan=lifespan,
    )
    app.state.config = config if config is not None else Config.from_env()
    app.state.engine = None
    app.state.sessionmaker = None
    app.state.clock = SystemClock()
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
