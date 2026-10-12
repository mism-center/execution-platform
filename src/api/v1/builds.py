"""Build endpoints — fire the envagent image builder for an approved model."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from core.errors import ConflictError, NotFoundError, OrchestrationError, ValidationError
from dependencies import get_build_service
from schemas.builds import BuildRequest, BuildResponse, BuildStatusResponse
from services.build_service import BuildInProgressError, BuildService

router = APIRouter(prefix="/builds", tags=["builds"])


@router.post("", response_model=BuildResponse, status_code=201)
async def create_build(
    body: BuildRequest,
    service: Annotated[BuildService, Depends(get_build_service)],
) -> BuildResponse:
    """Launch an envbuild agent Job for an APPROVED model resource.

    The model directory and its annotation package must exist on the iRODS
    PVC. One build per model: 409 while one is pending/running; a finished one
    is replaced. Poll with ``GET /builds/{resource_id}``.
    """
    try:
        return await service.launch(body)
    except LookupError as e:
        raise NotFoundError(detail=str(e)) from e
    except BuildInProgressError as e:
        raise ConflictError(detail=str(e)) from e
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e


@router.get("/{resource_id}", response_model=BuildStatusResponse)
async def get_build(
    resource_id: str,
    service: Annotated[BuildService, Depends(get_build_service)],
) -> BuildStatusResponse:
    """Live status of the model's builder Job.

    404 once the Job is gone — appstore TTL removes it 24h after it finishes.
    ``succeeded`` means the agent exited cleanly, not that an image verified.
    """
    try:
        status = await service.get_status(resource_id)
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e
    if status is None:
        raise NotFoundError(detail=f"No build found for resource {resource_id}")
    return status
