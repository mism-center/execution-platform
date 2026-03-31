"""Request / response schemas for run endpoints."""

from __future__ import annotations

from mism_registry import RunStatus
from pydantic import BaseModel, Field

from schemas.enums import PodPhase


class CreateRunRequest(BaseModel):
    """POST /api/v1/runs — execute a pre-created Run from the DAL.

    The Discovery Gateway creates the Run record (via prepare_run) in the
    shared Postgres database, then sends the run_id here.  The execution
    platform resolves the model, inputs, and resource requirements from
    the DAL.
    """

    run_id: str = Field(..., description="ID of an existing Run record in the DAL")


class RunResponse(BaseModel):
    """Representation of a run resource."""

    run_id: str
    sid: str
    status: RunStatus
    phase: PodPhase | None = None
    is_ready: bool | None = None
    url: str | None = None
    error: str | None = None


class RunListResponse(BaseModel):
    """GET /api/v1/runs — list of runs."""

    runs: list[RunResponse]
