"""API-level enums for the execution platform.

Note: RunStatus is reused from mism_registry (the DAL) — import it as
``from mism_registry import RunStatus``.  Only platform-specific enums
are defined here.
"""

from __future__ import annotations

from enum import Enum


class PodPhase(str, Enum):
    """Kubernetes pod phase as observed by the platform."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VivariumStatus(str, Enum):
    """User-facing status for a Vivarium PoC instance."""

    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    UNKNOWN = "unknown"
