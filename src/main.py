"""MISM Execution Platform — FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1 import runs
from core.errors import register_error_handlers
from core.logging import configure_logging
from core.settings import get_settings
from middleware.request_context import RequestContextMiddleware
from services.run_service import RunService

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 15


async def _poll_loop(service: RunService) -> None:
    logger.info(f"Batch run poller started (interval={_POLL_INTERVAL_SECONDS}s)")
    while True:
        try:
            await service.poll_batch_runs()
        except Exception:
            logger.warning("Poll loop: unhandled error during poll", exc_info=True)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    poll_task: asyncio.Task | None = None

    if settings.database_url:
        from dependencies import get_run_service
        poll_task = asyncio.create_task(_poll_loop(get_run_service()))

    yield

    if poll_task is not None:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            logger.info("Batch run poller stopped")

    # Clear cached singletons so tests stay isolated.
    from dependencies import _create_appstore, _create_dal

    _create_appstore.cache_clear()
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

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
