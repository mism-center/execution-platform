"""Request / response schemas for run endpoints."""

from __future__ import annotations

from mism_registry import RunStatus
from pydantic import BaseModel, Field

class CreateRunRequest(BaseModel):
    """POST /api/v1/runs — execute a pre-created Run from the DAL."""

    run_id: str = Field(..., description="ID of an existing Run record in the DAL")


class OutputResource(BaseModel):
    """An output dataset linked to a completed run."""

    resource_id: str
    location_uri: str


class FileInfo(BaseModel):
    """A single file in a run's output directory."""

    name: str
    size: int
    modified_at: str


class RunResponse(BaseModel):
    """Representation of a run resource."""

    run_id: str
    sid: str
    status: RunStatus
    mode: str | None = None  # "batch" or "interactive"
    phase: str | None = None
    is_ready: bool | None = None
    url: str | None = None  # Ambassador URL for interactive sessions
    error: str | None = None
    output_resources: list[OutputResource] = []


class RunListResponse(BaseModel):
    """GET /api/v1/runs — list of runs."""

    runs: list[RunResponse]
