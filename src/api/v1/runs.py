"""Model execution run endpoints — thin handlers delegating to RunService."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse

from core.errors import NotFoundError, OrchestrationError, ValidationError
from core.settings import get_settings
from dependencies import get_run_service
from schemas.runs import CreateRunRequest, FileInfo, RunListResponse, RunResponse
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
        mode="batch",
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


@router.get("/{run_id}/files", response_model=list[FileInfo])
async def list_run_files(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> list[FileInfo]:
    """List output files for a completed run."""
    run = service.get_run(run_id)
    if run is None:
        raise NotFoundError(detail=f"Run {run_id} not found")

    if not run.output_resources:
        return []

    settings = get_settings()
    output_uri = run.output_resources[0].location_uri
    output_dir = Path(settings.irods_mount_path) / output_uri.strip("/")

    if not output_dir.is_dir():
        return []

    files = []
    for entry in output_dir.iterdir():
        if entry.is_file():
            stat = entry.stat()
            files.append(FileInfo(
                name=entry.name,
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            ))
    return files


@router.get("/{run_id}/files/{filename}")
async def download_run_file(
    run_id: str,
    filename: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> FileResponse:
    """Download a specific output file from a completed run."""
    # Path traversal protection
    if ".." in filename or "/" in filename or "\\" in filename:
        raise ValidationError(detail="Invalid filename")

    run = service.get_run(run_id)
    if run is None:
        raise NotFoundError(detail=f"Run {run_id} not found")

    if not run.output_resources:
        raise NotFoundError(detail="Run has no output resources")

    settings = get_settings()
    output_uri = run.output_resources[0].location_uri
    file_path = Path(settings.irods_mount_path) / output_uri.strip("/") / filename

    # Verify resolved path stays within the output directory
    output_dir = Path(settings.irods_mount_path) / output_uri.strip("/")
    if not file_path.resolve().is_relative_to(output_dir.resolve()):
        raise ValidationError(detail="Invalid filename")

    if not file_path.is_file():
        raise NotFoundError(detail=f"File {filename} not found")

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


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
