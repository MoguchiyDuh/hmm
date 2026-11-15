from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from hmm.admin import router as admin
from hmm.areas import router as areas
from hmm.attendance import router as attendance
from hmm.catalog import router as catalog
from hmm.feed import router as feed
from hmm.formation import router as formation
from hmm.identity import router as identity
from hmm.mentorship import router as mentorship
from hmm.notifications import router as notifications
from hmm.onboarding import router as onboarding
from hmm.platform.clock import SystemClock
from hmm.platform.config import Config
from hmm.platform.db import create_engine, create_sessionmaker, migration_head
from hmm.platform.errors import register_exception_handlers
from hmm.platform.jobs import create_jobs_app, register_jobs
from hmm.platform.logs import RequestIdMiddleware
from hmm.platform.openapi import build_openapi, operation_id
from hmm.platform.storage import create_storage
from hmm.profiles import router as profiles
from hmm.projects import router as projects
from hmm.recommendations import router as recommendations
from hmm.results import router as results
from hmm.scores import router as scores


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
    app.state.storage = create_storage(config)
    engine = create_engine(config.database_url) if config.database_url else None
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine) if engine is not None else None
    app.state.jobs = (
        register_jobs(create_jobs_app(config.database_url)) if config.database_url else None
    )
    async with httpx.AsyncClient(timeout=10) as http:
        app.state.http = http
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()


def create_app(config: Config | None = None) -> FastAPI:
    app = FastAPI(
        title="hmm",
        version=version("hmm"),
        generate_unique_id_function=operation_id,
        separate_input_output_schemas=False,
        lifespan=lifespan,
    )
    app.state.config = config if config is not None else Config.from_env()
    app.state.engine = None
    app.state.sessionmaker = None
    app.state.jobs = None
    app.state.http = None
    app.state.storage = None
    app.state.clock = SystemClock()
    register_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", readyz, methods=["GET"], include_in_schema=False)
    for domain in (
        identity,
        profiles,
        areas,
        onboarding,
        scores,
        catalog,
        formation,
        results,
        recommendations,
        notifications,
        admin,
        feed,
        attendance,
        projects,
        mentorship,
    ):
        app.include_router(domain.router, prefix="/api/v1")

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi
    return app
