"""Run service — orchestrates model execution lifecycle.

All business logic for creating, querying, and cancelling runs lives here.
Endpoints are thin wrappers that delegate to this service.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from mism_registry import RunStatus

from core.settings import Settings
from orchestration.compute import Compute
from orchestration.models import (
    ContainerSpec,
    ResourceLimits,
    SystemSpec,
    VolumeMount,
)
from schemas.enums import PodPhase
from schemas.runs import RunResponse
from services.dal_service import DEFAULT_RESOURCE_REQUIREMENTS, DALService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """Internal result of a run creation — used by the endpoint to build the response."""

    run_id: str
    sid: str
    status: RunStatus
    url: str | None


class RunService:
    """Encapsulates all run-related business logic."""

    def __init__(self, dal: DALService, compute: Compute, settings: Settings) -> None:
        self._dal = dal
        self._compute = compute
        self._settings = settings

    def create_run(self, run_id: str) -> RunResult:
        """Execute a pre-created Run.

        The Discovery Gateway has already called prepare_run() in the shared
        database.  We resolve the model and inputs from the DAL, build the
        K8s spec, and launch the pod.
        """
        # 1. Fetch the Run from the shared DAL
        run = self._dal.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found in DAL")

        # 2. Fetch the model Resource to get the container image
        model = self._dal.get_resource(run.model_id)
        if model is None:
            raise ValueError(f"Model {run.model_id} not found in DAL")
        if not model.execution_ref:
            raise ValueError(
                f"Model {run.model_id} has no execution_ref (container image)"
            )

        # 3. Fetch input Resources to get their iRODS paths
        input_paths = self._resolve_input_paths(run.input_resource_ids)

        # 4. Resolve resource requirements from model metadata
        resource_reqs = model.metadata.get(
            "resource_requirements", DEFAULT_RESOURCE_REQUIREMENTS
        )
        cpus = resource_reqs.get("cpus", DEFAULT_RESOURCE_REQUIREMENTS["cpus"])
        memory = resource_reqs.get("memory", DEFAULT_RESOURCE_REQUIREMENTS["memory"])

        # 5. Pre-generate sid for K8s correlation
        sid = uuid.uuid4().hex

        # 6. Build system spec
        system = self._build_system_spec(
            model_image=model.execution_ref,
            model_name=model.name,
            model_id=model.id,
            run_id=run_id,
            sid=sid,
            input_paths=input_paths,
            cpus=cpus,
            memory=memory,
        )

        # 7. Launch on K8s
        try:
            result = self._compute.start(system)
        except Exception as e:
            self._safe_cancel(run_id)
            raise RuntimeError(f"Failed to launch pod: {e}") from e

        # 8. Mark running (non-blocking — also persists sid in notes)
        try:
            self._dal.mark_running(run_id)
        except Exception:
            logger.warning(f"Non-blocking: failed to update run {run_id} to running")

        return RunResult(
            run_id=run_id,
            sid=result.sid,
            status=RunStatus.RUNNING,
            url=result.url,
        )

    def get_run(self, run_id: str) -> RunResponse | None:
        """Get a run resource, enriched with live K8s status if active."""
        run = self._dal.get_run(run_id)
        if run is None:
            return None

        status = RunStatus(run.status.value)
        sid = run.notes or None
        phase: PodPhase | None = None
        is_ready: bool | None = None
        url: str | None = None

        if status in (RunStatus.REGISTERED, RunStatus.RUNNING) and sid:
            status, phase, is_ready, url = self._sync_live_status(run_id, sid, status)

        return RunResponse(
            run_id=run_id,
            sid=sid or "",
            status=status,
            phase=phase,
            is_ready=is_ready,
            url=url,
            error=run.error_message or None,
        )

    def list_runs(self) -> list[RunResponse]:
        """List all runs. Lightweight — no live K8s enrichment."""
        runs = self._dal.list_all_runs()
        return [
            RunResponse(
                run_id=run.id,
                sid=run.notes or "",
                status=RunStatus(run.status.value),
            )
            for run in runs
        ]

    def delete_run(self, run_id: str) -> bool:
        """Cancel a run and delete its K8s resources."""
        run = self._dal.get_run(run_id)
        if run is None:
            return False

        sid = run.notes
        status = RunStatus(run.status.value)

        if status in (RunStatus.REGISTERED, RunStatus.RUNNING):
            try:
                self._dal.cancel(run_id)
            except Exception:
                logger.warning(f"Failed to cancel run {run_id} in DAL")

        if sid:
            try:
                self._compute.delete(sid)
            except Exception:
                logger.warning(f"Failed to delete K8s resources for sid={sid}")

        return True

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

    def _build_system_spec(
        self,
        *,
        model_image: str,
        model_name: str,
        model_id: str,
        run_id: str,
        sid: str,
        input_paths: list[tuple[str, str]],
        cpus: str,
        memory: str,
    ) -> SystemSpec:
        """Build a K8s SystemSpec from resolved DAL data."""
        volumes = self._build_volumes(input_paths, run_id)
        env = {
            "MODEL_ID": model_id,
            "RUN_ID": run_id,
            "INPUT_PATH": "/input",
            "OUTPUT_PATH": "/output",
        }
        container = ContainerSpec(
            name=model_name.lower().replace(" ", "-")[:63],
            image=model_image,
            env=env,
            limits=ResourceLimits(cpus=cpus, memory=memory),
            requests=ResourceLimits(cpus="0.5", memory="512Mi"),
            volumes=volumes,
        )
        return SystemSpec(
            app_name="mism-run",
            containers=[container],
            identifier=sid,
            namespace=self._settings.namespace,
            service_account=self._settings.service_account,
            ambassador_enabled=False,
        )

    def _build_volumes(
        self, input_paths: list[tuple[str, str]], run_id: str
    ) -> list[VolumeMount]:
        """Build input + output volume mounts from resolved Resource paths."""
        pvc = self._settings.irods_pvc_name
        volumes: list[VolumeMount] = []

        # Input mounts — one per input Resource
        for i, (_rid, uri) in enumerate(input_paths):
            volumes.append(
                VolumeMount(
                    name=f"input-{i}",
                    mount_path=f"/input/{i}" if len(input_paths) > 1 else "/input",
                    pvc_name=pvc,
                    sub_path=uri.lstrip("/"),
                    read_only=True,
                )
            )

        # Output mount — auto-generated subdirectory per run
        output_sub = f"{self._settings.output_base_dir.strip('/')}/{run_id}"
        volumes.append(
            VolumeMount(
                name="output-data",
                mount_path="/output",
                pvc_name=pvc,
                sub_path=output_sub,
                read_only=False,
            )
        )

        return volumes

    def _sync_live_status(
        self, run_id: str, sid: str, current: RunStatus
    ) -> tuple[RunStatus, PodPhase | None, bool | None, str | None]:
        """Check live K8s pod status and auto-update DAL if terminal."""
        kube_status = self._compute.status(sid)
        if kube_status is None:
            return current, None, None, None

        phase = kube_status.phase
        is_ready = kube_status.is_ready
        url = kube_status.url
        status = current

        if phase == PodPhase.SUCCEEDED and current != RunStatus.COMPLETED:
            try:
                self._dal.mark_succeeded(run_id)
                status = RunStatus.COMPLETED
            except Exception:
                logger.warning(f"Failed to auto-complete run {run_id}")
        elif phase == PodPhase.FAILED and current != RunStatus.FAILED:
            try:
                self._dal.mark_failed(run_id, "Pod terminated with non-zero exit")
                status = RunStatus.FAILED
            except Exception:
                logger.warning(f"Failed to auto-fail run {run_id}")

        return status, phase, is_ready, url

    def _safe_cancel(self, run_id: str) -> None:
        """Cancel a run, swallowing errors."""
        try:
            self._dal.cancel(run_id)
        except Exception:
            logger.warning(f"Failed to cancel run {run_id} after K8s error")
