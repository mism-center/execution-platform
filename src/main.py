"""MISM Execution Platform — FastAPI application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1 import annotations, runs
from core.errors import register_error_handlers
from core.logging import configure_logging
from core.settings import get_settings
from middleware.request_context import RequestContextMiddleware

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 15


async def _poll_loop(name: str, poll_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    logger.info(f"{name} poller started (interval={_POLL_INTERVAL_SECONDS}s)")
    while True:
        try:
            await poll_fn()
        except Exception:
            logger.warning(f"{name} poll loop: unhandled error", exc_info=True)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    tasks: list[asyncio.Task] = []

    if get_settings().database_url:
        from dependencies import get_annotation_service, get_run_service
        tasks.append(asyncio.create_task(
            _poll_loop("batch run", get_run_service().poll_batch_runs)
        ))
        tasks.append(asyncio.create_task(
            _poll_loop("annotation", get_annotation_service().poll_annotating_resources)
        ))

    yield

    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if tasks:
        logger.info("All pollers stopped")

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
    app.include_router(annotations.router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
