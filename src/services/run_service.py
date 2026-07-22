"""Run service — orchestrates model execution lifecycle.

All business logic for creating, querying, and cancelling runs lives here.
Endpoints are thin wrappers that delegate to this service.

All K8s orchestration is delegated to the appstore:
- Batch: K8s Jobs via /api/v1/jobs/
- Interactive: K8s Deployments via /api/v1/containers/

DAL calls are synchronous (SQLAlchemy + psycopg) and are offloaded to a
thread pool via run_in_executor to avoid blocking the async event loop.
Appstore calls use the async AppstoreClient.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from functools import partial

from mism_registry import Resource, ResourceType, RunStatus
from mism_registry.types import Compute, EntryPoint

from core.settings import Settings
from schemas.runs import OutputResource, RunResponse
from services.appstore_client import AppstoreClient
from services.dal_service import DEFAULT_RESOURCE_REQUIREMENTS, DALService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """Internal result of a run creation — used by the endpoint to build the response."""

    run_id: str
    sid: str
    status: RunStatus
    url: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveResult:
    """Result of launching an interactive session."""

    run_id: str
    sid: str
    url: str
    status: RunStatus


class RunService:
    """Encapsulates all run-related business logic."""

    def __init__(
        self,
        dal: DALService,
        appstore: AppstoreClient,
        settings: Settings,
    ) -> None:
        self._dal = dal
        self._appstore = appstore
        self._settings = settings

    # ------------------------------------------------------------------
    # Internal helpers for notes (JSON state stored on Run.notes)
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_notes(
        sid: str,
        output_resource_id: str,
        output_uri: str,
        mode: str,
        url: str = "",
    ) -> str:
        return json.dumps({
            "sid": sid,
            "output_resource_id": output_resource_id,
            "output_uri": output_uri,
            "mode": mode,
            "url": url,
        })

    @staticmethod
    def _unpack_notes(notes: str) -> dict:
        """Parse notes — handles both legacy (plain sid) and new (JSON)."""
        if not notes:
            return {}
        try:
            return json.loads(notes)
        except (json.JSONDecodeError, TypeError):
            return {"sid": notes}

    # ------------------------------------------------------------------
    # Output Resource helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_output_resource() -> tuple[str, str]:
        """Pre-generate an output Resource ID and location_uri.

        Convention: ``<resource-id>/v1`` on the PVC.
        """
        resource_id = str(uuid.uuid4())
        location_uri = f"{resource_id}/v1"
        return resource_id, location_uri

    # ------------------------------------------------------------------
    # Thread-pool helper for sync DAL calls
    # ------------------------------------------------------------------

    @staticmethod
    async def _in_executor(fn, *args, **kwargs):
        """Run a synchronous function in the default thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------
    # Run-schema helpers (entrypoint / container / compute → launch args)
    # ------------------------------------------------------------------

    @staticmethod
    def _require_image(run) -> str:
        """Pull the container image off the Run snapshot; hard-error if missing."""
        if run.container is None or not run.container.image_name:
            raise ValueError(
                f"Run {run.id} has no container.image_name — model must ship a "
                "Container recipe with image_name set for the exec platform to launch it"
            )
        return run.container.image_name

    @staticmethod
    def _resolve_compute(compute: Compute | None) -> tuple[str, str]:
        """Map a model's Compute to the (cpus, memory) strings the appstore wants.

        Falls back to DEFAULT_RESOURCE_REQUIREMENTS for missing fields.
        memory_gb is emitted as a Ki-style suffix (e.g. 2.0 → "2.0Gi").
        Compute is not snapshotted onto the Run today (see TECH_DEBT TD-005),
        so callers pass model.compute at execution time.
        """
        default_cpus = DEFAULT_RESOURCE_REQUIREMENTS["cpus"]
        default_memory = DEFAULT_RESOURCE_REQUIREMENTS["memory"]
        if compute is None:
            return default_cpus, default_memory
        cpus = str(compute.cpu_cores) if compute.cpu_cores is not None else default_cpus
        memory = f"{compute.memory_gb}Gi" if compute.memory_gb is not None else default_memory
        return cpus, memory

    @staticmethod
    def _render_batch_command(entrypoint: EntryPoint | None, parameters: dict) -> list[str]:
        """Render the Run's entrypoint into a K8s container command list.

        EntryPoint.to_cli() produces a shell-quoted string (positional args
        first, then options; bool args become presence flags; every value is
        shlex.quote'd). We wrap it in `sh -c` so the container gets a shell
        that respects that quoting and any operators (>, |) the annotator
        embedded in EntryPoint.command.
        """
        if entrypoint is None:
            raise ValueError(
                "Run has no entrypoint — Discovery must call prepare_run with an "
                "entrypoint_index before the exec platform can launch"
            )
        rendered = entrypoint.to_cli(values=parameters or {})
        return ["sh", "-c", rendered]

    # ------------------------------------------------------------------
    # Batch execution (via appstore /api/v1/jobs/)
    # ------------------------------------------------------------------

    async def create_run(self, run_id: str) -> RunResult:
        """Execute a pre-created Run as a headless batch Job.

        Image and command come from the Run snapshot (container.image_name,
        entrypoint) that prepare_run stamped on. Compute still reads from the
        model since Run doesn't carry it yet (TECH_DEBT TD-005).
        """
        run = await self._in_executor(self._dal.get_run, run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        model = await self._in_executor(self._dal.get_resource, run.model_id)
        if model is None:
            raise ValueError(f"Model {run.model_id} not found in DAL")

        image = self._require_image(run)
        command = self._render_batch_command(run.entrypoint, run.parameters)
        cpus, memory = self._resolve_compute(model.compute)

        input_paths = await self._in_executor(
            self._resolve_input_paths, run.input_resource_ids
        )

        output_resource_id, output_uri = self._generate_output_resource()

        sid = uuid.uuid4().hex
        pvc = self._settings.irods_pvc_name

        pvc_mounts = self._build_pvc_mounts(input_paths, output_uri, pvc)

        env = {
            "MODEL_ID": model.id,
            "RUN_ID": run_id,
            "INPUT_PATH": "/input",
            "OUTPUT_PATH": "/output",
        }

        try:
            result = await self._appstore.launch_job(
                name=f"mism-{model.name[:12]}-{run_id[:8]}".lower().replace(" ", "-"),
                identifier=sid,
                image=image,
                cpus=cpus,
                memory=memory,
                env=env,
                command=command,
                pvc_mounts=pvc_mounts,
            )
        except Exception as e:
            await self._in_executor(self._safe_cancel, run_id)
            raise RuntimeError(f"Failed to launch job: {e}") from e

        notes = self._pack_notes(
            result.sid, output_resource_id, output_uri, mode="batch"
        )
        try:
            await self._in_executor(self._dal.mark_running, run_id, notes)
        except Exception:
            logger.warning(f"Non-blocking: failed to update run {run_id} to running", exc_info=True)

        return RunResult(
            run_id=run_id,
            sid=result.sid,
            status=RunStatus.RUNNING,
            url=None,
        )

    # ------------------------------------------------------------------
    # Interactive session (via appstore /api/v1/containers/)
    # ------------------------------------------------------------------

    async def create_interactive(self, run_id: str) -> InteractiveResult:
        """Launch an interactive session for a Run via the appstore.

        Image comes from the Run snapshot (container.image_name); compute
        reads from model.compute. Interactive sessions run the image's default
        entrypoint (typically a Jupyter server), so run.entrypoint is not
        consulted here.
        """
        jupyter_token = secrets.token_urlsafe(32)

        run = await self._in_executor(self._dal.get_run, run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        model = await self._in_executor(self._dal.get_resource, run.model_id)
        if model is None:
            raise ValueError(f"Model {run.model_id} not found in DAL")

        image = self._require_image(run)
        cpus_str, memory = self._resolve_compute(model.compute)
        cpus = float(cpus_str)

        input_paths = await self._in_executor(
            self._resolve_input_paths, run.input_resource_ids
        )

        output_resource_id, output_uri = self._generate_output_resource()

        pvc = self._settings.irods_pvc_name
        pvc_mounts = self._build_pvc_mounts(
            input_paths, output_uri, pvc,
            input_prefix="/data/input", output_mount="/data/output",
        )

        env = {
            "MODEL_ID": model.id,
            "RUN_ID": run_id,
            "JUPYTER_TOKEN": jupyter_token,
            "OUTPUT_PATH": "/data/output",
        }

        session = await self._appstore.launch(
            image=image,
            name=f"{model.name[:12]}-{run_id[:8]}".lower().replace(" ", "-"),
            cpus=cpus,
            memory=memory,
            env=env,
            pvc_mounts=pvc_mounts,
        )

        path = session.url.split("/private/", 1)[-1] if "/private/" in session.url else ""
        ambassador_base = self._settings.ambassador_url.rstrip("/")
        base_url = f"{ambassador_base}/private/{path}" if path else session.url
        url = f"{base_url}?token={jupyter_token}" if jupyter_token else base_url

        notes = self._pack_notes(
            session.sid, output_resource_id, output_uri,
            mode="interactive", url=url,
        )
        try:
            await self._in_executor(self._dal.mark_running, run_id, notes)
        except Exception:
            logger.warning(f"Non-blocking: failed to update run {run_id} to running", exc_info=True)

        return InteractiveResult(
            run_id=run_id,
            sid=session.sid,
            url=url,
            status=RunStatus.RUNNING,
        )

    # ------------------------------------------------------------------
    # Complete interactive session
    # ------------------------------------------------------------------

    async def complete_interactive(self, run_id: str) -> RunResponse:
        """Mark an interactive session done: register outputs and kill the container."""
        run = await self._in_executor(self._dal.get_run, run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        notes = self._unpack_notes(run.notes)
        mode = notes.get("mode")
        status = RunStatus(run.status.value)

        if mode != "interactive":
            raise ValueError(
                f"Run {run_id} is not an interactive session (mode={mode})"
            )
        if status != RunStatus.RUNNING:
            raise ValueError(
                f"Run {run_id} cannot be completed from status={status.value}"
            )

        sid = notes.get("sid")

        try:
            await self._in_executor(self._complete_run, run_id, notes)
        except Exception as e:
            raise RuntimeError(f"Failed to complete run {run_id} in DAL: {e}") from e

        if sid:
            try:
                await self._appstore.delete_container(sid)
            except Exception:
                logger.warning(
                    f"Failed to delete container sid={sid} for run {run_id} — "
                    "container may already be gone",
                    exc_info=True,
                )

        result = await self.get_run(run_id)
        if result is None:
            raise RuntimeError(f"Run {run_id} not found after completion")
        return result

    # ------------------------------------------------------------------
    # Query & lifecycle
    # ------------------------------------------------------------------

    async def get_run(self, run_id: str) -> RunResponse | None:
        """Get a run resource, enriched with live status if active."""
        run = await self._in_executor(self._dal.get_run, run_id)
        if run is None:
            return None

        status = RunStatus(run.status.value)
        notes = self._unpack_notes(run.notes)
        sid = notes.get("sid")
        mode = notes.get("mode")
        url: str | None = notes.get("url") or None

        phase: str | None = None
        is_ready: bool | None = None

        if status in (RunStatus.REGISTERED, RunStatus.RUNNING) and sid and mode == "batch":
            status, phase, is_ready = await self._sync_batch_status(run_id, sid, status, notes)

        output_resources = await self._in_executor(
            self._resolve_output_resources, run.output_resource_ids
        )

        return RunResponse(
            run_id=run_id,
            sid=sid or "",
            status=status,
            mode=mode,
            phase=phase,
            is_ready=is_ready,
            url=url,
            error=run.error_message or None,
            output_resources=output_resources,
        )

    async def list_runs(self) -> list[RunResponse]:
        """List all runs with mode and output info."""
        runs = await self._in_executor(self._dal.list_all_runs)
        results = []
        for run in runs:
            notes = self._unpack_notes(run.notes)
            output_resources = await self._in_executor(
                self._resolve_output_resources, run.output_resource_ids
            )
            results.append(
                RunResponse(
                    run_id=run.id,
                    sid=notes.get("sid", ""),
                    status=RunStatus(run.status.value),
                    mode=notes.get("mode"),
                    url=notes.get("url") or None,
                    output_resources=output_resources,
                )
            )
        return results

    async def delete_run(self, run_id: str) -> bool:
        """Cancel a run and delete its K8s resources."""
        run = await self._in_executor(self._dal.get_run, run_id)
        if run is None:
            return False

        notes = self._unpack_notes(run.notes)
        sid = notes.get("sid")
        mode = notes.get("mode")
        status = RunStatus(run.status.value)

        if status in (RunStatus.REGISTERED, RunStatus.RUNNING):
            try:
                await self._in_executor(self._dal.cancel, run_id)
            except Exception:
                logger.warning(f"Failed to cancel run {run_id} in DAL", exc_info=True)

        if sid:
            try:
                if mode == "interactive":
                    await self._appstore.delete_container(sid)
                else:
                    await self._appstore.delete_job(sid)
            except Exception:
                logger.warning(f"Failed to delete K8s resources for sid={sid}", exc_info=True)

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_pvc_mounts(
        self,
        input_paths: list[tuple[str, str]],
        output_uri: str,
        pvc: str,
        input_prefix: str = "/input",
        output_mount: str = "/output",
    ) -> list[dict]:
        """Build PVC mount dicts for the appstore API."""
        mounts = []
        for i, (_rid, uri) in enumerate(input_paths):
            mount_path = f"{input_prefix}/{i}" if len(input_paths) > 1 else input_prefix
            mounts.append({
                "pvc": pvc,
                "mount_path": mount_path,
                "sub_path": uri.strip("/"),
                "read_only": True,
            })
        mounts.append({
            "pvc": pvc,
            "mount_path": output_mount,
            "sub_path": output_uri.strip("/"),
            "read_only": False,
        })
        return mounts

    def _resolve_output_resources(self, resource_ids: list[str]) -> list[OutputResource]:
        """Look up output Resources by ID and return their location_uri."""
        results = []
        for rid in resource_ids:
            resource = self._dal.get_resource(rid)
            if resource is not None:
                results.append(OutputResource(
                    resource_id=resource.id,
                    location_uri=resource.location_uri,
                ))
        return results

    def _resolve_input_paths(self, input_resource_ids: list[str]) -> list[tuple[str, str]]:
        """Resolve input Resource IDs to (resource_id, location_uri) pairs."""
        paths: list[tuple[str, str]] = []
        for rid in input_resource_ids:
            resource = self._dal.get_resource(rid)
            if resource is None:
                raise ValueError(f"Input resource {rid} not found in DAL")
            if not resource.location_uri:
                raise ValueError(f"Input resource {rid} has no location_uri")
            paths.append((rid, resource.location_uri))
        return paths

    async def _sync_batch_status(
        self,
        run_id: str,
        sid: str,
        current: RunStatus,
        notes: dict,
    ) -> tuple[RunStatus, str | None, bool | None]:
        """Check live batch Job status via appstore and auto-update DAL."""
        job_status = await self._appstore.job_status(sid)
        if job_status is None:
            return current, None, None

        phase = job_status.phase
        status = current

        if job_status.status == "succeeded" and current != RunStatus.COMPLETED:
            try:
                await self._in_executor(self._complete_run, run_id, notes)
                status = RunStatus.COMPLETED
            except Exception:
                logger.warning(f"Failed to auto-complete run {run_id}", exc_info=True)
        elif job_status.status == "failed" and current != RunStatus.FAILED:
            try:
                await self._in_executor(
                    self._dal.mark_failed, run_id, "Job terminated with non-zero exit"
                )
                status = RunStatus.FAILED
            except Exception:
                logger.warning(f"Failed to auto-fail run {run_id}", exc_info=True)

        is_ready = job_status.status == "running"
        return status, phase, is_ready

    def _complete_run(self, run_id: str, notes: dict) -> None:
        """Register output Resource and mark run as completed."""
        output_resource_id = notes.get("output_resource_id", "")
        output_uri = notes.get("output_uri", "")

        output_resources: list[Resource] = []
        if output_resource_id and output_uri:
            output_resource = Resource(
                id=output_resource_id,
                name=f"output-{run_id[:8]}",
                resource_type=ResourceType.DATASET,
                location_uri=output_uri,
            )
            output_resources.append(output_resource)

        self._dal.mark_succeeded(run_id, output_resources=output_resources)

    async def poll_batch_runs(self) -> None:
        """Called by the background poller — sync all RUNNING batch runs against appstore."""
        runs = await self._in_executor(self._dal.list_all_runs)
        for run in runs:
            notes = self._unpack_notes(run.notes or "")
            if notes.get("mode") != "batch":
                continue
            if RunStatus(run.status.value) != RunStatus.RUNNING:
                continue
            sid = notes.get("sid")
            if not sid:
                continue
            try:
                await self._sync_batch_status(run.id, sid, RunStatus.RUNNING, notes)
            except Exception:
                logger.warning(f"Poller: failed to sync run {run.id}", exc_info=True)

    def _safe_cancel(self, run_id: str) -> None:
        """Cancel a run, swallowing errors."""
        try:
            self._dal.cancel(run_id)
        except Exception:
            logger.warning(f"Failed to cancel run {run_id} after K8s error", exc_info=True)
