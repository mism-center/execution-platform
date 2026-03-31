"""Request / response schemas for PoC Vivarium endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from schemas.enums import PodPhase, VivariumStatus
from schemas.types import K8sQuantity


class CreateVivariumRequest(BaseModel):
    """POST /api/v1/poc/vivarium — launch interactive Jupyter + vivarium-core."""

    cpus: K8sQuantity = Field("1", description="CPU limit")
    memory: K8sQuantity = Field("1Gi", description="Memory limit")


class ExecuteVivariumRequest(BaseModel):
    """POST /api/v1/poc/vivarium/execute — headless notebook execution."""

    cpus: K8sQuantity = Field("1", description="CPU limit")
    memory: K8sQuantity = Field("1Gi", description="Memory limit")


class VivariumResponse(BaseModel):
    """Representation of a Vivarium PoC instance (interactive)."""

    sid: str
    status: VivariumStatus
    phase: PodPhase | None = None
    is_ready: bool = False
    url: str
    jupyter_token: str | None = None


class VivariumExecutionResponse(BaseModel):
    """Representation of a headless Vivarium execution."""

    sid: str
    status: VivariumStatus
    phase: PodPhase | None = None
    output_path: str | None = None
    error: str | None = None
