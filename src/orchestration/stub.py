"""Stub compute backend for local development without a K8s cluster.

Returns fake results so the API can be exercised end-to-end locally.
Satisfies the ``Compute`` protocol.
"""

from __future__ import annotations

import logging

from orchestration.compute import StartResult, SystemStatus
from orchestration.models import SystemSpec
from schemas.enums import PodPhase

logger = logging.getLogger(__name__)


class StubCompute:
    """In-memory stub that tracks systems without touching Kubernetes."""

    def __init__(self) -> None:
        self._systems: dict[str, SystemSpec] = {}

    def start(self, system: SystemSpec) -> StartResult:
        self._systems[system.identifier] = system
        logger.info(f"[stub] Started system: {system.full_name}")
        return StartResult(name=system.full_name, sid=system.identifier, url=None)

    def status(self, sid: str) -> SystemStatus | None:
        system = self._systems.get(sid)
        if system is None:
            return None
        return SystemStatus(
            sid=sid,
            name=system.full_name,
            phase=PodPhase.RUNNING,
            is_ready=True,
            url=None,
        )

    def delete(self, sid: str) -> None:
        self._systems.pop(sid, None)
        logger.info(f"[stub] Deleted system sid={sid}")
