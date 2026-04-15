"""Model execution run endpoints — thin handlers delegating to RunService."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from core.errors import NotFoundError, OrchestrationError, ValidationError
from dependencies import get_run_service
from schemas.runs import CreateRunRequest, RunListResponse, RunResponse
from services.run_service import RunService

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: CreateRunRequest,
    response: Response,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Execute a pre-created Run from the DAL."""
    try:
        result = service.create_run(body.run_id)
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e

    response.headers["Location"] = f"/api/v1/runs/{result.run_id}"
    return RunResponse(
        run_id=result.run_id,
        sid=result.sid,
        status=result.status,
        url=result.url,
    )


@router.get("", response_model=RunListResponse)
async def list_runs(
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunListResponse:
    """List all runs."""
    return RunListResponse(runs=service.list_runs())


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Get a run resource, including live K8s status if active."""
    result = service.get_run(run_id)
    if result is None:
        raise NotFoundError(detail=f"Run {run_id} not found")
    return result


@router.post("/{run_id}/interactive", status_code=201)
async def create_interactive(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> dict:
    """Launch an interactive session for a Run."""
    try:
        result = service.create_interactive(run_id)
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e

    return {
        "run_id": result.run_id,
        "sid": result.sid,
        "url": result.url,
        "status": result.status.value,
    }


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> None:
    """Cancel a run and delete its K8s resources."""
    deleted = service.delete_run(run_id)
    if not deleted:
        raise NotFoundError(detail=f"Run {run_id} not found")
