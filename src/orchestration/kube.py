"""Kubernetes compute backend for MISM execution pods.

Adapted from Tycho's ``KubernetesCompute`` — provides start, status, and
delete operations against the Kubernetes API.  Renders pod and service
manifests from Jinja2 templates then applies them via the ``kubernetes``
Python client.

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
    """Manages Deployment + Service lifecycle on Kubernetes.

    Satisfies the ``Compute`` protocol.
    """

    def __init__(self, namespace: str = "default") -> None:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            k8s_config.load_incluster_config()
        else:
            k8s_config.load_kube_config()

        api_client = k8s_client.ApiClient()
        self.core_api = k8s_client.CoreV1Api(api_client)
        self.apps_api = k8s_client.AppsV1Api(api_client)
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
        """Create a Deployment + Service for the given system spec."""
        # 1. Render pod manifest and convert to PodTemplateSpec
        pod_docs = self._render_template("pod.yaml", {"system": system})
        pod_template = self._to_pod_template_spec(pod_docs[0])

        deploy_response = self._create_deployment(system, pod_template)

        # 3. Create Service (with Ambassador annotation)
        self._create_service(
            system,
            deployment_name=deploy_response.metadata.name,
            deployment_uid=deploy_response.metadata.uid,
        )

        url = system.ambassador_prefix if system.ambassador_enabled else None
        result = StartResult(name=system.full_name, sid=system.identifier, url=url)
        logger.info(f"Started system: {result}")
        return result

    @staticmethod
    def _to_pod_template_spec(pod_manifest: dict[str, Any]) -> dict[str, Any]:
        """Convert a rendered Pod manifest into a PodTemplateSpec dict."""
        return {
            "metadata": pod_manifest.get("metadata", {}),
            "spec": pod_manifest.get("spec", {}),
        }

    def _create_deployment(
        self, system: SystemSpec, pod_template: dict[str, Any]
    ) -> Any:
        deployment_spec = k8s_client.V1DeploymentSpec(
            replicas=1,
            template=pod_template,
            selector=k8s_client.V1LabelSelector(
                match_labels={
                    "mism-guid": system.identifier,
                    "username": system.username,
                }
            ),
        )
        deployment = k8s_client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=k8s_client.V1ObjectMeta(
                name=system.full_name,
                labels={
                    "mism-guid": system.identifier,
                    "executor": "mism-exec",
                    "username": system.username,
                    "app-name": system.app_name,
                },
            ),
            spec=deployment_spec,
        )
        response = self.apps_api.create_namespaced_deployment(
            body=deployment, namespace=self.namespace
        )
        logger.info(f"Deployment created: {system.full_name}")
        return response

    def _create_service(
        self,
        system: SystemSpec,
        deployment_name: str,
        deployment_uid: str,
    ) -> None:
        svc_docs = self._render_template(
            "service.yaml",
            {
                "system": system,
                "deployment_name": deployment_name,
                "deployment_uid": deployment_uid,
            },
        )
        svc_manifest = svc_docs[0]
        self.core_api.create_namespaced_service(
            body=svc_manifest, namespace=self.namespace
        )
        logger.info(f"Service created: {system.full_name}")


    def status(self, sid: str) -> SystemStatus | None:
        """Get status of a system by its identifier (sid)."""
        try:
            response = self.apps_api.list_namespaced_deployment(
                namespace=self.namespace,
                label_selector=f"mism-guid={sid}",
            )
        except ApiException:
            logger.exception(f"Failed to get status for sid={sid}")
            return None

        if not response.items:
            return None

        item = response.items[0]
        desired = item.status.replicas or 0
        ready = item.status.ready_replicas or 0
        is_ready = ready >= desired and desired > 0

        phase = self._get_pod_phase(sid)

        app_name = item.metadata.labels.get("app-name", "")
        username = item.metadata.labels.get("username", "")
        url = f"/private/{app_name}/{username}/{sid}/"

        return SystemStatus(
            sid=sid,
            name=item.metadata.name,
            phase=phase,
            is_ready=is_ready,
            url=url,
        )

    def _get_pod_phase(self, sid: str) -> PodPhase:
        """Get the phase of the pod(s) backing a deployment."""
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
            self.apps_api.delete_collection_namespaced_deployment(
                namespace=self.namespace, label_selector=label
            )
            self.apps_api.delete_collection_namespaced_replica_set(
                namespace=self.namespace, label_selector=label
            )
            self.core_api.delete_collection_namespaced_pod(
                namespace=self.namespace, label_selector=label
            )
            self.core_api.delete_collection_namespaced_service(
                namespace=self.namespace, label_selector=label
            )
            logger.info(f"Deleted system sid={sid}")
        except ApiException:
            logger.exception(f"Failed to delete system sid={sid}")
            raise
