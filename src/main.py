"""MISM Execution Platform — FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1 import poc, runs
from core.errors import register_error_handlers
from core.logging import configure_logging
from core.settings import get_settings
from middleware.request_context import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hook for resource management."""
    # Startup — nothing to initialise for now; add K8s client cleanup here later.
    yield
    # Shutdown — clear cached singletons so tests stay isolated.
    from dependencies import _create_compute, _create_dal

    _create_compute.cache_clear()
    _create_dal.cache_clear()
    get_settings.cache_clear()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level="DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title="MISM Execution Platform",
        version="0.1.0",
        description=(
            "Orchestrates model execution on Kubernetes for the MISM ecosystem. "
            "Provides run lifecycle management via the DAL (mism-registry) and "
            "K8s pod orchestration adapted from HeLx/Tycho."
        ),
        lifespan=lifespan,
    )

    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(poc.router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
