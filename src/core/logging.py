"""Structured logging configuration with request-ID injection.

Call ``configure_logging()`` once at app startup.  Every log record will
include a ``request_id`` field populated from the context var set by
``RequestContextMiddleware``.
"""

from __future__ import annotations

import logging
import logging.config


class RequestIDFilter(logging.Filter):
    """Inject the current request ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        from middleware.request_context import request_id_var

        record.request_id = request_id_var.get("-")  # type: ignore[attr-defined]
        return True


def configure_logging(*, level: str = "INFO") -> None:
    """Apply structured logging config with request-ID support."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_id": {
                    "()": RequestIDFilter,
                },
            },
            "formatters": {
                "standard": {
                    "format": (
                        "%(asctime)s %(levelname)-8s %(name)s "
                        "request_id=%(request_id)s — %(message)s"
                    ),
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "filters": ["request_id"],
                    "stream": "ext://sys.stderr",
                },
            },
            "root": {
                "level": level.upper(),
                "handlers": ["console"],
            },
        }
    )
