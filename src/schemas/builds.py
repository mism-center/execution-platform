"""Request / response schemas for envbuild (envagent image builder) endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BuildRequest(BaseModel):
    """POST /api/v1/builds — fire the envagent builder against an approved model."""

    resource_id: str = Field(
        ..., description="ID of the model resource to build (must be APPROVED)"
    )
    annotation_subpath: str = Field(
        "metadata-package",
        description="Annotation package dir, relative to the model's directory",
    )
    prompt: str | None = Field(
        None, description="Extra instruction appended to the agent's task (PROMPT)"
    )


class BuildResponse(BaseModel):
    """Response after the builder Job is created.

    Builds are tracked by ``resource_id`` (one per model at a time); ``run_id``
    is unique per launch and is what envagent stamps on its verdict rows.
    """

    resource_id: str
    job_name: str
    run_id: str
    model_repo: str
    status: str


class BuildStatusResponse(BaseModel):
    """Live status of the builder Job.

    ``succeeded`` means the agent exited cleanly, NOT that an image was
    verified — the verdict is in envbuild-work:/records/verdicts.jsonl.
    """

    resource_id: str
    job_name: str
    status: str
    phase: str
    exit_code: int | None = None
