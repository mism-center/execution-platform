"""Run service — orchestrates model execution lifecycle.

All business logic for creating, querying, and cancelling runs lives here.
Endpoints are thin wrappers that delegate to this service.

All K8s orchestration is delegated to the appstore:
- Batch: K8s Jobs via /api/v1/jobs/
- Interactive: K8s Deployments via /api/v1/containers/
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass

from mism_registry import Resource, ResourceType, RunStatus

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
    def _generate_output_resource(model_name: str, run_id: str) -> tuple[str, str]:
        """Pre-generate an output Resource ID and location_uri.

        Convention: ``<resource-id>/v1`` on the PVC.
        """
        resource_id = str(uuid.uuid4())
        location_uri = f"{resource_id}/v1"
        return resource_id, location_uri

    # ------------------------------------------------------------------
    # Batch execution (via appstore /api/v1/jobs/)
    # ------------------------------------------------------------------

    def create_run(self, run_id: str) -> RunResult:
        """Execute a pre-created Run as a headless batch Job."""
        run = self._dal.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        model = self._dal.get_resource(run.model_id)
        if model is None:
            raise ValueError(f"Model {run.model_id} not found in DAL")
        if not model.execution_ref:
            raise ValueError(
                f"Model {run.model_id} has no execution_ref (container image)"
            )

        input_paths = self._resolve_input_paths(run.input_resource_ids)

        resource_reqs = model.metadata.get(
            "resource_requirements", DEFAULT_RESOURCE_REQUIREMENTS
        )
        cpus = resource_reqs.get("cpus", DEFAULT_RESOURCE_REQUIREMENTS["cpus"])
        memory = resource_reqs.get("memory", DEFAULT_RESOURCE_REQUIREMENTS["memory"])

        output_resource_id, output_uri = self._generate_output_resource(
            model.name, run_id
        )

        sid = uuid.uuid4().hex
        pvc = self._settings.irods_pvc_name

        # Build PVC mounts
        pvc_mounts = self._build_pvc_mounts(input_paths, output_uri, pvc)

        env = {
            "MODEL_ID": model.id,
            "RUN_ID": run_id,
            "INPUT_PATH": "/input",
            "OUTPUT_PATH": "/output",
        }
        command = model.metadata.get("command")

        try:
            result = self._appstore.launch_job(
                name=f"mism-{model.name[:12]}-{run_id[:8]}".lower().replace(" ", "-"),
                identifier=sid,
                image=model.execution_ref,
                cpus=cpus,
                memory=memory,
                env=env,
                command=command,
                pvc_mounts=pvc_mounts,
            )
        except Exception as e:
            self._safe_cancel(run_id)
            raise RuntimeError(f"Failed to launch job: {e}") from e

        notes = self._pack_notes(
            result.sid, output_resource_id, output_uri, mode="batch"
        )
        try:
            self._dal.mark_running(run_id, notes=notes)
        except Exception:
            logger.warning(f"Non-blocking: failed to update run {run_id} to running")

        return RunResult(
            run_id=run_id,
            sid=result.sid,
            status=RunStatus.RUNNING,
            url=None,
        )

    # ------------------------------------------------------------------
    # Interactive session (via appstore /api/v1/containers/)
    # ------------------------------------------------------------------

    def create_interactive(self, run_id: str) -> InteractiveResult:
        """Launch an interactive session for a Run via the appstore."""
        jupyter_token = secrets.token_urlsafe(32)

        run = self._dal.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        model = self._dal.get_resource(run.model_id)
        if model is None:
            raise ValueError(f"Model {run.model_id} not found in DAL")
        if not model.execution_ref:
            raise ValueError(
                f"Model {run.model_id} has no execution_ref (container image)"
            )

        input_paths = self._resolve_input_paths(run.input_resource_ids)

        resource_reqs = model.metadata.get(
            "resource_requirements", DEFAULT_RESOURCE_REQUIREMENTS
        )
        cpus = float(resource_reqs.get("cpus", DEFAULT_RESOURCE_REQUIREMENTS["cpus"]))
        memory = resource_reqs.get("memory", DEFAULT_RESOURCE_REQUIREMENTS["memory"])

        output_resource_id, output_uri = self._generate_output_resource(
            model.name, run_id
        )

        pvc = self._settings.irods_pvc_name
        pvc_mounts = self._build_pvc_mounts(
            input_paths, output_uri, pvc,
            input_prefix="/data/input", output_mount="/data/output",
        )

        env = {
            "MODEL_ID": model.id,
            "RUN_ID": run_id,
            "JUPYTER_TOKEN": jupyter_token,
        }

        session = self._appstore.launch(
            image=model.execution_ref,
            name=f"{model.name[:12]}-{run_id[:8]}".lower().replace(" ", "-"),
            cpus=cpus,
            memory=memory,
            env=env,
            pvc_mounts=pvc_mounts,
        )

        # Build the user-facing URL via Ambassador ingress
        path = session.url.split("/private/", 1)[-1] if "/private/" in session.url else ""
        ambassador_base = self._settings.ambassador_url.rstrip("/")
        base_url = f"{ambassador_base}/private/{path}" if path else session.url
        url = f"{base_url}?token={jupyter_token}" if jupyter_token else base_url

        notes = self._pack_notes(
            session.sid, output_resource_id, output_uri,
            mode="interactive", url=url,
        )
        try:
            self._dal.mark_running(run_id, notes=notes)
        except Exception:
            logger.warning(f"Non-blocking: failed to update run {run_id} to running")

        return InteractiveResult(
            run_id=run_id,
            sid=session.sid,
            url=url,
            status=RunStatus.RUNNING,
        )

    # ------------------------------------------------------------------
    # Query & lifecycle
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> RunResponse | None:
        """Get a run resource, enriched with live status if active."""
        run = self._dal.get_run(run_id)
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
            status, phase, is_ready = self._sync_batch_status(run_id, sid, status, notes)
        # Interactive status comes from notes (url) — no live polling needed for MVP

        output_resources = self._resolve_output_resources(run.output_resource_ids)

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

    def list_runs(self) -> list[RunResponse]:
        """List all runs with mode and output info."""
        runs = self._dal.list_all_runs()
        results = []
        for run in runs:
            notes = self._unpack_notes(run.notes)
            output_resources = self._resolve_output_resources(run.output_resource_ids)
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

    def delete_run(self, run_id: str) -> bool:
        """Cancel a run and delete its K8s resources."""
        run = self._dal.get_run(run_id)
        if run is None:
            return False

        notes = self._unpack_notes(run.notes)
        sid = notes.get("sid")
        mode = notes.get("mode")
        status = RunStatus(run.status.value)

        if status in (RunStatus.REGISTERED, RunStatus.RUNNING):
            try:
                self._dal.cancel(run_id)
            except Exception:
                logger.warning(f"Failed to cancel run {run_id} in DAL")

        if sid:
            try:
                if mode == "interactive":
                    self._appstore.delete_container(sid)
                else:
                    self._appstore.delete_job(sid)
            except Exception:
                logger.warning(f"Failed to delete K8s resources for sid={sid}")

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

    def _sync_batch_status(
        self,
        run_id: str,
        sid: str,
        current: RunStatus,
        notes: dict,
    ) -> tuple[RunStatus, str | None, bool | None]:
        """Check live batch Job status via appstore and auto-update DAL."""
        job_status = self._appstore.job_status(sid)
        if job_status is None:
            return current, None, None

        phase = job_status.phase
        status = current

        if job_status.status == "succeeded" and current != RunStatus.COMPLETED:
            try:
                self._complete_run(run_id, notes)
                status = RunStatus.COMPLETED
            except Exception:
                logger.warning(f"Failed to auto-complete run {run_id}")
        elif job_status.status == "failed" and current != RunStatus.FAILED:
            try:
                self._dal.mark_failed(run_id, "Job terminated with non-zero exit")
                status = RunStatus.FAILED
            except Exception:
                logger.warning(f"Failed to auto-fail run {run_id}")

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

    def _safe_cancel(self, run_id: str) -> None:
        """Cancel a run, swallowing errors."""
        try:
            self._dal.cancel(run_id)
        except Exception:
            logger.warning(f"Failed to cancel run {run_id} after K8s error")
