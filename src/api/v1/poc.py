"""Proof-of-concept endpoints — Vivarium Jupyter notebook.

Two modes:
- POST /poc/vivarium          → interactive Jupyter UI (bonus)
- POST /poc/vivarium/execute  → headless notebook execution (main PoC)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from core.errors import NotFoundError, OrchestrationError
from dependencies import get_vivarium_service
from schemas.poc import (
    CreateVivariumRequest,
    ExecuteVivariumRequest,
    VivariumExecutionResponse,
    VivariumResponse,
)
from services.vivarium_service import VivariumService

router = APIRouter(prefix="/poc/vivarium", tags=["poc"])


@router.post("/execute", response_model=VivariumExecutionResponse, status_code=201)
async def execute_vivarium(
    body: ExecuteVivariumRequest,
    response: Response,
    service: Annotated[VivariumService, Depends(get_vivarium_service)],
) -> VivariumExecutionResponse:
    """Execute a vivarium notebook headlessly.

    Launches a container that auto-runs the sample notebook, captures
    output to a local directory, and exits.  Simulates the model
    execution flow without interactive UI.
    """
    try:
        result = service.execute(body)
    except Exception as e:
        raise OrchestrationError(detail=f"Failed to launch Vivarium execution: {e}") from e

    response.headers["Location"] = f"/api/v1/poc/vivarium/execute/{result.sid}"
    return result


@router.get("/execute/{sid}", response_model=VivariumExecutionResponse)
async def get_execution(
    sid: str,
    service: Annotated[VivariumService, Depends(get_vivarium_service)],
) -> VivariumExecutionResponse:
    """Get the status of a headless Vivarium execution."""
    result = service.get_execution(sid)
    if result is None:
        raise NotFoundError(detail=f"Vivarium execution {sid} not found")
    return result


@router.post("", response_model=VivariumResponse, status_code=201)
async def create_vivarium(
    body: CreateVivariumRequest,
    response: Response,
    service: Annotated[VivariumService, Depends(get_vivarium_service)],
) -> VivariumResponse:
    """Launch an interactive Jupyter notebook with vivarium-core."""
    try:
        result = service.create_instance(body)
    except Exception as e:
        raise OrchestrationError(detail=f"Failed to launch Vivarium pod: {e}") from e

    response.headers["Location"] = f"/api/v1/poc/vivarium/{result.sid}"
    return result


@router.get("/{sid}", response_model=VivariumResponse)
async def get_vivarium(
    sid: str,
    service: Annotated[VivariumService, Depends(get_vivarium_service)],
) -> VivariumResponse:
    """Get the current state of an interactive Vivarium instance."""
    result = service.get_instance(sid)
    if result is None:
        raise NotFoundError(detail=f"Vivarium session {sid} not found")
    return result


@router.delete("/{sid}", status_code=204)
async def delete_vivarium(
    sid: str,
    service: Annotated[VivariumService, Depends(get_vivarium_service)],
) -> None:
    """Terminate a Vivarium instance (interactive or headless)."""
    deleted = service.delete_instance(sid)
    if not deleted:
        raise NotFoundError(detail=f"Vivarium session {sid} not found")
