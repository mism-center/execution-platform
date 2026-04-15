"""Kubernetes compute backend for MISM execution pods.

Uses K8s Jobs (not Deployments) for run-to-completion semantics.
Batch containers run once and exit — no restart, no Service, no Ambassador.

Implements the ``Compute`` protocol.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from orchestration.compute import StartResult, SystemStatus
from orchestration.models import SystemSpec
from schemas.enums import PodPhase

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


class KubernetesCompute:
    """Manages Job lifecycle on Kubernetes.

    Satisfies the ``Compute`` protocol.
    """

    def __init__(self, namespace: str = "default") -> None:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            k8s_config.load_incluster_config()
        else:
            k8s_config.load_kube_config()

        api_client = k8s_client.ApiClient()
        self.core_api = k8s_client.CoreV1Api(api_client)
        self.batch_api = k8s_client.BatchV1Api(api_client)
        self.namespace = namespace
        logger.info(f"KubernetesCompute initialised for namespace={namespace}")

    # Template rendering

    @staticmethod
    def _render_template(template_name: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        template_path = _TEMPLATE_DIR / template_name
        with open(template_path) as f:
            tmpl = Template(f.read())
        tmpl.globals["now"] = datetime.datetime.utcnow
        rendered = tmpl.render(**context)
        return list(yaml.safe_load_all(rendered))

    def start(self, system: SystemSpec) -> StartResult:
        """Create a Job for the given system spec."""
        pod_docs = self._render_template("pod.yaml", {"system": system})
        pod_template = self._to_pod_template_spec(pod_docs[0])

        self._create_job(system, pod_template)

        result = StartResult(name=system.full_name, sid=system.identifier, url=None)
        logger.info(f"Started job: {result}")
        return result

    @staticmethod
    def _to_pod_template_spec(pod_manifest: dict[str, Any]) -> dict[str, Any]:
        """Convert a rendered Pod manifest into a PodTemplateSpec dict."""
        return {
            "metadata": pod_manifest.get("metadata", {}),
            "spec": pod_manifest.get("spec", {}),
        }

    def _create_job(
        self, system: SystemSpec, pod_template: dict[str, Any]
    ) -> Any:
        job_spec = k8s_client.V1JobSpec(
            template=pod_template,
            backoff_limit=0,
        )
        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=system.full_name,
                labels={
                    "mism-guid": system.identifier,
                    "executor": "mism-exec",
                    "username": system.username,
                    "app-name": system.app_name,
                },
            ),
            spec=job_spec,
        )
        response = self.batch_api.create_namespaced_job(
            body=job, namespace=self.namespace
        )
        logger.info(f"Job created: {system.full_name}")
        return response

    def status(self, sid: str) -> SystemStatus | None:
        """Get status of a system by its identifier (sid)."""
        try:
            response = self.batch_api.list_namespaced_job(
                namespace=self.namespace,
                label_selector=f"mism-guid={sid}",
            )
        except ApiException:
            logger.exception(f"Failed to get status for sid={sid}")
            return None

        if not response.items:
            return None

        job = response.items[0]
        job_status = job.status

        # Check Job-level conditions first
        phase = PodPhase.PENDING
        is_ready = False

        if job_status.succeeded and job_status.succeeded > 0:
            phase = PodPhase.SUCCEEDED
        elif job_status.failed and job_status.failed > 0:
            phase = PodPhase.FAILED
        elif job_status.active and job_status.active > 0:
            phase = PodPhase.RUNNING
            is_ready = True
        else:
            # Fall back to pod-level inspection
            phase = self._get_pod_phase(sid)

        return SystemStatus(
            sid=sid,
            name=job.metadata.name,
            phase=phase,
            is_ready=is_ready,
            url=None,
        )

    def _get_pod_phase(self, sid: str) -> PodPhase:
        """Get the phase of the pod(s) backing a job."""
        try:
            pods = self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"mism-guid={sid}",
            )
            if not pods.items:
                return PodPhase.UNKNOWN

            pod = pods.items[0]
            raw_phase = (pod.status.phase or "Unknown").lower()

            # Check for completed/failed containers
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.terminated:
                        exit_code = cs.state.terminated.exit_code
                        return PodPhase.SUCCEEDED if exit_code == 0 else PodPhase.FAILED

            try:
                return PodPhase(raw_phase)
            except ValueError:
                return PodPhase.UNKNOWN
        except ApiException:
            return PodPhase.UNKNOWN

    def delete(self, sid: str) -> None:
        """Delete all resources associated with a system identifier."""
        label = f"mism-guid={sid}"
        try:
            self.batch_api.delete_collection_namespaced_job(
                namespace=self.namespace,
                label_selector=label,
                propagation_policy="Background",
            )
            # Pods owned by the Job are garbage-collected automatically,
            # but clean up any stragglers.
            self.core_api.delete_collection_namespaced_pod(
                namespace=self.namespace, label_selector=label
            )
            logger.info(f"Deleted job sid={sid}")
        except ApiException:
            logger.exception(f"Failed to delete job sid={sid}")
            raise
