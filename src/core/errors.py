"""Centralised error types and FastAPI exception handlers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class PlatformError(Exception):
    code: str
    detail: str
    status_code: int = 500


class NotFoundError(PlatformError):
    def __init__(self, detail: str) -> None:
        super().__init__(code="not_found", detail=detail, status_code=404)


class ValidationError(PlatformError):
    def __init__(self, detail: str) -> None:
        super().__init__(code="validation_error", detail=detail, status_code=400)


class OrchestrationError(PlatformError):
    def __init__(self, detail: str) -> None:
        super().__init__(code="orchestration_error", detail=detail, status_code=502)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def _platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "detail": exc.detail}},
        )
