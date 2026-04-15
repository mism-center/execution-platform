"""Vivarium PoC service — manages Jupyter + vivarium-core instances.

Supports two modes:
- Interactive: serves Jupyter UI for manual exploration (bonus).
- Headless: auto-executes a notebook, captures output (main PoC goal).

Self-contained for now — will integrate with DAL shortly.
"""

from __future__ import annotations

import logging
import secrets
import uuid

from core.settings import Settings
from orchestration.compute import Compute
from orchestration.models import (
    ContainerSpec,
    ResourceLimits,
    SystemSpec,
    VolumeMount,
)
from schemas.enums import PodPhase, VivariumStatus
from schemas.poc import (
    CreateVivariumRequest,
    ExecuteVivariumRequest,
    VivariumExecutionResponse,
    VivariumResponse,
)

logger = logging.getLogger(__name__)


class VivariumService:
    """Encapsulates all Vivarium PoC business logic."""

    def __init__(self, compute: Compute, settings: Settings) -> None:
        self._compute = compute
        self._settings = settings
        self._sessions: dict[str, dict[str, str]] = {}
        self._executions: dict[str, dict[str, str]] = {}

    def create_instance(self, request: CreateVivariumRequest) -> VivariumResponse:
        """Launch an interactive Jupyter + vivarium-core instance."""
        jupyter_token = secrets.token_urlsafe(32)
        system = self._build_interactive_spec(request, jupyter_token)

        result = self._compute.start(system)

        self._sessions[result.sid] = {
            "token": jupyter_token,
            "url": result.url or "",
        }

        return VivariumResponse(
            sid=result.sid,
            status=VivariumStatus.STARTING,
            url=f"{result.url}?token={jupyter_token}",
            jupyter_token=jupyter_token,
        )

    def execute(self, request: ExecuteVivariumRequest) -> VivariumExecutionResponse:
        """Launch a headless notebook execution.

        The container runs `jupyter nbconvert --execute` on the sample
        notebook, writes the executed notebook + generated artifacts to
        /output, then exits.  Simulates a model execution flow.
        """
        execution_id = uuid.uuid4().hex
        output_sub = f"{self._settings.poc_output_base_dir.strip('/')}/{execution_id}"

        system = self._build_headless_spec(request, execution_id, output_sub)

        result = self._compute.start(system)

        self._executions[result.sid] = {
            "execution_id": execution_id,
            "output_path": output_sub,
        }

        return VivariumExecutionResponse(
            sid=result.sid,
            status=VivariumStatus.STARTING,
            output_path=output_sub,
        )

    def get_instance(self, sid: str) -> VivariumResponse | None:
        """Get the current state of an interactive Vivarium instance."""
        session = self._sessions.get(sid)
        if session is None:
            return None

        kube_status = self._compute.status(sid)
        if kube_status is None:
            return VivariumResponse(
                sid=sid,
                status=VivariumStatus.UNKNOWN,
                phase=PodPhase.UNKNOWN,
                is_ready=False,
                url=session["url"],
            )

        status = self._map_status(kube_status.phase, kube_status.is_ready)
        base_url = kube_status.url or session["url"]
        url = f"{base_url}?token={session['token']}"

        return VivariumResponse(
            sid=sid,
            status=status,
            phase=kube_status.phase,
            is_ready=kube_status.is_ready,
            url=url,
        )

    def get_execution(self, sid: str) -> VivariumExecutionResponse | None:
        """Get the current state of a headless execution."""
        execution = self._executions.get(sid)
        if execution is None:
            return None

        kube_status = self._compute.status(sid)
        if kube_status is None:
            return VivariumExecutionResponse(
                sid=sid,
                status=VivariumStatus.UNKNOWN,
                output_path=execution["output_path"],
            )

        phase = kube_status.phase
        if phase == PodPhase.SUCCEEDED:
            status = VivariumStatus.READY
        elif phase == PodPhase.FAILED:
            status = VivariumStatus.FAILED
        elif phase in (PodPhase.PENDING, PodPhase.RUNNING):
            status = VivariumStatus.STARTING
        else:
            status = VivariumStatus.UNKNOWN

        return VivariumExecutionResponse(
            sid=sid,
            status=status,
            phase=phase,
            output_path=execution["output_path"],
        )

    def delete_instance(self, sid: str) -> bool:
        """Terminate an interactive or headless Vivarium instance."""
        found = sid in self._sessions or sid in self._executions
        if not found:
            return False

        try:
            self._compute.delete(sid)
        except Exception:
            logger.warning(f"Failed to delete K8s resources for Vivarium sid={sid}")

        self._sessions.pop(sid, None)
        self._executions.pop(sid, None)
        return True

    def _build_interactive_spec(
        self, request: CreateVivariumRequest, jupyter_token: str
    ) -> SystemSpec:
        container = ContainerSpec(
            name="vivarium-jupyter",
            image=self._settings.vivarium_image,
            env={
                "JUPYTER_TOKEN": jupyter_token,
                "JUPYTER_ENABLE_LAB": "yes",
            },
            command=[
                "start-notebook.sh",
                f"--NotebookApp.token={jupyter_token}",
                "--NotebookApp.allow_origin=*",
                "--ServerApp.allow_remote_access=true",
            ],
            limits=ResourceLimits(cpus=request.cpus, memory=request.memory),
            requests=ResourceLimits(cpus="0.5", memory="1Gi"),
        )
        return SystemSpec(
            app_name="vivarium",
            containers=[container],
            namespace=self._settings.namespace,
            service_account=self._settings.service_account,
            security_context={"run_as_user": "1000", "run_as_group": "100"},
        )

    def _build_headless_spec(
        self,
        request: ExecuteVivariumRequest,
        execution_id: str,
        output_sub: str,
    ) -> SystemSpec:
        notebook = self._settings.poc_notebook_path
        container = ContainerSpec(
            name="vivarium-execute",
            image=self._settings.vivarium_image,
            env={
                "EXECUTION_ID": execution_id,
            },
            command=[
                "jupyter",
                "nbconvert",
                "--to", "notebook",
                "--execute",
                "--ExecutePreprocessor.timeout=600",
                "--output-dir=/output",
                notebook,
            ],
            limits=ResourceLimits(cpus=request.cpus, memory=request.memory),
            requests=ResourceLimits(cpus="0.5", memory="1Gi"),
            volumes=[
                VolumeMount(
                    name="output",
                    mount_path="/output",
                    pvc_name=self._settings.poc_output_pvc,
                    sub_path=output_sub,
                    read_only=False,
                ),
            ],
        )
        return SystemSpec(
            app_name="vivarium-exec",
            containers=[container],
            namespace=self._settings.namespace,
            service_account=self._settings.service_account,
            security_context={"run_as_user": "1000", "run_as_group": "100"},
        )

    @staticmethod
    def _map_status(phase: PodPhase, is_ready: bool) -> VivariumStatus:
        if is_ready:
            return VivariumStatus.READY
        if phase == PodPhase.FAILED:
            return VivariumStatus.FAILED
        if phase in (PodPhase.PENDING, PodPhase.RUNNING):
            return VivariumStatus.STARTING
        return VivariumStatus.UNKNOWN
