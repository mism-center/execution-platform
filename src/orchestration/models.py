"""Simplified system model for MISM execution pods.

Adapted from Tycho's model.py — stripped of app-registry, docker-compose,
gitea, NFS home-dir, and interactive-session concerns.  Purpose-built for
launching model-execution and PoC containers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceLimits:
    """Immutable resource constraints for a container."""

    cpus: str | None = None
    memory: str | None = None
    gpus: int | None = None
    ephemeral_storage: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class VolumeMount:
    """A single volume mount into the container."""

    name: str
    mount_path: str
    pvc_name: str
    sub_path: str | None = None
    read_only: bool = False


@dataclass(slots=True, kw_only=True)
class ContainerSpec:
    """Specification for one container in the pod."""

    name: str
    image: str
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    ports: list[int] = field(default_factory=list)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    requests: ResourceLimits = field(default_factory=ResourceLimits)
    volumes: list[VolumeMount] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class SystemSpec:
    """Complete specification for a MISM execution pod.

    This is the execution-platform equivalent of Tycho's ``System``.  It carries
    only what the platform needs to create a Deployment + Service + Ambassador
    mapping on Kubernetes.
    """

    app_name: str
    containers: list[ContainerSpec]
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)
    username: str = "mism"
    namespace: str = "default"
    service_account: str = "default"
    ambassador_enabled: bool = True
    security_context: dict[str, str] = field(default_factory=dict)
    gpu_resource_name: str = "nvidia.com/gpu"

    @property
    def full_name(self) -> str:
        """Deployment / service name: ``<app_name>-<identifier>``."""
        return f"{self.app_name}-{self.identifier}"

    @property
    def primary_port(self) -> int:
        """Port of the first container's first exposed port (for Service)."""
        for c in self.containers:
            if c.ports:
                return c.ports[0]
        return 8888  # sensible default for Jupyter-style containers

    @property
    def ambassador_prefix(self) -> str:
        return f"/private/{self.app_name}/{self.username}/{self.identifier}/"
