"""Compute protocol and typed result models.

Defines the contract that any orchestration backend must implement,
plus the typed dataclasses for start/status results (replacing raw dicts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orchestration.models import SystemSpec
from schemas.enums import PodPhase


@dataclass(frozen=True, slots=True, kw_only=True)
class StartResult:
    """Returned by Compute.start()."""

    name: str
    sid: str
    url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemStatus:
    """Returned by Compute.status()."""

    sid: str
    name: str
    phase: PodPhase
    is_ready: bool
    url: str | None = None


@runtime_checkable
class Compute(Protocol):
    """Abstraction over the compute backend (K8s, stub, etc.)."""

    def start(self, system: SystemSpec) -> StartResult: ...

    def status(self, sid: str) -> SystemStatus | None: ...

    def delete(self, sid: str) -> None: ...
