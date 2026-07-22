"""Request / response schemas for annotation endpoints."""

from __future__ import annotations

from mism_registry import ResourceRegistrationStatus
from pydantic import BaseModel, Field


class AnnotateRequest(BaseModel):
    """POST /api/v1/annotations — kick off an annotation job for a model resource."""

    resource_id: str = Field(
        ..., description="ID of the model resource to annotate (must be DRAFT or ANNOTATION_FAILED)"
    )
    image: str = Field(..., description="Annotator container image")
    prompt: str = Field(..., description="Annotation prompt passed to the agent")
    extra_env: dict[str, str] | None = Field(
        None, description="Additional environment variables passed to the annotation pod"
    )
    command: list[str] | None = Field(
        None, description="Override the container entrypoint (e.g. ['pi', '--stream=all', ...])"
    )
    cpus: str = Field("1", description="CPU request for the annotation pod")
    memory: str = Field("4Gi", description="Memory request for the annotation pod")


class AnnotateResponse(BaseModel):
    """Response after successfully kicking off an annotation job."""

    resource_id: str
    sid: str
    registration_status: ResourceRegistrationStatus
