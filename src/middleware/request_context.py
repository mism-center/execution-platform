"""Request context middleware — assigns request IDs and logs timing.

Every inbound request gets a unique ``x-request-id`` (or preserves one
supplied by the caller).  The ID is stored in a ``contextvars.ContextVar``
so that all log records emitted during the request include it automatically
via ``RequestIDFilter``.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Context var — accessible from any code running within the request scope.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Injects a request ID and logs request start / completion with timing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Accept caller-provided ID or generate one.
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(rid)

        logger.info("request_started method=%s path=%s", request.method, request.url.path)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_failed method=%s path=%s duration_ms=%.1f",
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        finally:
            request_id_var.reset(token)

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "request_completed method=%s path=%s status_code=%d duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )

        response.headers[REQUEST_ID_HEADER] = rid
        return response
