"""Model execution run endpoints — thin handlers delegating to RunService."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse

from core.errors import NotFoundError, OrchestrationError, ValidationError
from core.settings import Settings, get_settings
from dependencies import get_run_service
from schemas.runs import CreateRunRequest, FileInfo, RunListResponse, RunResponse
from services.run_service import RunService

router = APIRouter(prefix="/runs", tags=["runs"])

_PLACEHOLDER_NAMES: frozenset[str] = frozenset({".gitignore", ".gitkeep", ".keep"})


async def _exists_with_retry(check: Callable[[], bool], settings: Settings) -> bool:
    """Bounded retry absorbing the iRODS PVC cross-pod visibility lag.

    The run's execution pod and this API pod mount the same PVC from
    different pods, so a file/directory can briefly be invisible through
    this pod's mount right after the run is marked succeeded. Mirrors the
    same fix applied in model-discovery's
    ``RegistryService._metadata_package_dir``.
    """
    for attempt in range(1, settings.run_files_retry_max_attempts + 1):
        if check():
            return True
        if attempt < settings.run_files_retry_max_attempts:
            await asyncio.sleep(settings.run_files_retry_backoff_seconds * attempt)
    return False


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: CreateRunRequest,
    response: Response,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Execute a pre-created Run from the DAL."""
    try:
        result = await service.create_run(body.run_id)
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
    return RunListResponse(runs=await service.list_runs())


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Get a run resource, including live K8s status if active."""
    result = await service.get_run(run_id)
    if result is None:
        raise NotFoundError(detail=f"Run {run_id} not found")
    return result


@router.get("/{run_id}/files", response_model=list[FileInfo])
async def list_run_files(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> list[FileInfo]:
    """List output files for a completed run."""
    run = await service.get_run(run_id)
    if run is None:
        raise NotFoundError(detail=f"Run {run_id} not found")

    if not run.output_resources:
        return []

    settings = get_settings()
    output_uri = run.output_resources[0].location_uri
    output_dir = Path(settings.irods_mount_path) / output_uri.strip("/")

    if not await _exists_with_retry(output_dir.is_dir, settings):
        return []

    files = []
    for entry in output_dir.iterdir():
        if entry.is_file() and entry.name not in _PLACEHOLDER_NAMES:
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

    run = await service.get_run(run_id)
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

    if not await _exists_with_retry(file_path.is_file, settings):
        raise NotFoundError(detail=f"File {filename} not found")

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
    )


@router.post("/{run_id}/complete", response_model=RunResponse)
async def complete_interactive(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Complete an interactive session: register outputs and terminate the container."""
    try:
        result = await service.complete_interactive(run_id)
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e
    return result


@router.post("/{run_id}/interactive", response_model=RunResponse, status_code=201)
async def create_interactive(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunResponse:
    """Launch an interactive session for a Run."""
    try:
        result = await service.create_interactive(run_id)
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e

    return RunResponse(
        run_id=result.run_id,
        sid=result.sid,
        status=result.status,
        mode="interactive",
        url=result.url,
    )


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    service: Annotated[RunService, Depends(get_run_service)],
) -> None:
    """Cancel a run and delete its K8s resources."""
    deleted = await service.delete_run(run_id)
    if not deleted:
        raise NotFoundError(detail=f"Run {run_id} not found")
