"""Build service — fires the envagent image builder (envbuild) for a model.

Phase 1 is fire-and-forget: ExecAPI asks appstore for one K8s Job shaped like
envagent's ``deploy/agent-job.yaml``. Builds are keyed by the model's resource
id (appstore identifier ``build-<resource_id>``), so there is at most one build
Job per model. Nothing is persisted; status is read live from appstore. The
agent itself creates the
Kaniko and verification pods (as the ``envbuild`` ServiceAccount) and writes
its verdict to the envbuild-work PVC.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import uuid
from functools import partial
from pathlib import Path

from mism_registry import ResourceRegistrationStatus, ResourceType

from core.settings import Settings
from core.storage import pvc_subpath
from schemas.builds import BuildRequest, BuildResponse, BuildStatusResponse
from services.appstore_client import AppstoreClient
from services.dal_service import DALService

logger = logging.getLogger(__name__)

# Mount points inside the agent pod, fixed by envagent's Job template.
MODELS_MOUNT = "/models"
WORK_MOUNT = "/work"
DOCKER_CONFIG_MOUNT = "/home/node/.docker"  # harness image runs as user `node`

# Prefix keeps the build's mism-guid label distinct from the annotation Job's,
# which is the bare resource_id: appstore's job_status/delete_job select by
# that label, and delete_job removes every match.
BUILD_IDENTIFIER_PREFIX = "build-"
JOB_NAME = "envbuild"  # appstore names the Job f"{name}-{identifier}"

# Resource ids are uuids. The id ends up in the Job name ("envbuild-build-<id>",
# a DNS label of at most 63 chars) and in appstore's label selector, so anything
# outside lowercase alphanumerics/dashes, or longer than 48 chars, is refused.
_RESOURCE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")


class BuildInProgressError(Exception):
    """A build Job for this model is still pending or running."""


def build_identifier(resource_id: str) -> str:
    return f"{BUILD_IDENTIFIER_PREFIX}{resource_id}"


class BuildService:
    """Launches envbuild agent Jobs through appstore."""

    def __init__(
        self,
        dal: DALService,
        appstore: AppstoreClient,
        settings: Settings,
    ) -> None:
        self._dal = dal
        self._appstore = appstore
        self._settings = settings

    @staticmethod
    async def _in_executor(fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    async def launch(self, request: BuildRequest) -> BuildResponse:
        """Validate the model and create the builder Job.

        Raises LookupError (unknown resource), ValueError (not buildable),
        BuildInProgressError (a build for this model is still active) or
        RuntimeError (appstore refused/failed).
        """
        if not _RESOURCE_ID.match(request.resource_id):
            raise LookupError(f"Resource {request.resource_id} not found")
        resource = await self._in_executor(self._dal.get_resource, request.resource_id)
        if resource is None:
            raise LookupError(f"Resource {request.resource_id} not found")
        if resource.resource_type != ResourceType.MODEL:
            raise ValueError(
                f"Resource {request.resource_id} is a {resource.resource_type.value}, not a model"
            )
        # The agent's L3 rung runs the model's own code with no sandbox beyond
        # a network deny, so only human-approved models are built.
        if resource.registration_status != ResourceRegistrationStatus.APPROVED:
            raise ValueError(
                f"Resource {request.resource_id} must be approved before building "
                f"(status={resource.registration_status.value})"
            )

        model_rel = pvc_subpath(resource.location_uri, self._settings.irods_mount_path)
        annotation_rel = _relative_subpath(request.annotation_subpath)
        await self._in_executor(self._check_files, model_rel, annotation_rel)

        identifier = build_identifier(resource.id)
        await self._clear_previous_build(identifier)

        run_id = uuid.uuid4().hex
        model_repo = f"{MODELS_MOUNT}/{model_rel}"
        image = self._settings.envbuild_agent_image

        env = {
            "MODEL_ID": f"mism:model/{resource.id}",
            "MODEL_REPO": model_repo,
            "ANNOTATION": f"{model_repo}/{annotation_rel}",
            "ENVBUILD_OUTPUTS": f"{WORK_MOUNT}/records",
            "ENVBUILD_WORK_PVC": self._settings.envbuild_work_pvc,
            "ENVBUILD_WORK_MOUNT": WORK_MOUNT,
            "ENVBUILD_MODELS_PVC": self._settings.irods_pvc_name,
            "ENVBUILD_MODELS_MOUNT": MODELS_MOUNT,
            # Fresh per launch and stamped on every attempt/verdict row as
            # run.run_id, so rebuilds of the same model stay distinguishable.
            "ENVBUILD_RUN_ID": run_id,
            "ENVBUILD_HARNESS_IMAGE": image,
        }
        if request.prompt:
            env["PROMPT"] = request.prompt

        try:
            result = await self._appstore.launch_job(
                name=JOB_NAME,
                identifier=identifier,
                image=image,
                cpus=self._settings.envbuild_cpus,
                memory=self._settings.envbuild_memory,
                env=env,
                pvc_mounts=[
                    {
                        "pvc": self._settings.envbuild_work_pvc,
                        "mount_path": WORK_MOUNT,
                        "sub_path": "",
                        "read_only": False,
                    },
                    {
                        "pvc": self._settings.irods_pvc_name,
                        "mount_path": MODELS_MOUNT,
                        "sub_path": "",
                        "read_only": True,
                    },
                ],
                service_account=self._settings.envbuild_service_account,
                # ConfigMap first, Secret last: a tuning knob must never be
                # able to shadow a credential.
                env_from=[
                    {
                        "kind": "configmap",
                        "name": self._settings.envbuild_tuning_configmap,
                        "optional": True,
                    },
                    {"kind": "secret", "name": self._settings.envbuild_llm_secret},
                ],
                secret_mounts=[
                    {
                        "secret": self._settings.envbuild_registry_secret,
                        "mount_path": DOCKER_CONFIG_MOUNT,
                        "items": {".dockerconfigjson": "config.json"},
                    }
                ],
                ttl_seconds_after_finished=self._settings.envbuild_ttl_seconds,
                security_context={
                    "allow_privilege_escalation": False,
                    "drop_capabilities": ["ALL"],
                },
            )
        except Exception as e:
            raise RuntimeError(f"Failed to launch envbuild job: {e}") from e

        logger.info(
            f"envbuild launched: resource={resource.id} run_id={run_id} job={result.name}"
        )
        return BuildResponse(
            resource_id=resource.id,
            job_name=result.name,
            run_id=run_id,
            model_repo=model_repo,
            status=result.status,
        )

    async def get_status(self, resource_id: str) -> BuildStatusResponse | None:
        """Live status of the model's build Job; None if there is none (or the
        TTL already removed it)."""
        if not _RESOURCE_ID.match(resource_id):
            return None
        try:
            status = await self._appstore.job_status(build_identifier(resource_id))
        except Exception as e:
            raise RuntimeError(f"Failed to read envbuild job status: {e}") from e
        if status is None:
            return None
        return BuildStatusResponse(
            resource_id=resource_id,
            job_name=status.name,
            status=status.status,
            phase=status.phase,
            exit_code=status.exit_code,
        )

    async def _clear_previous_build(self, identifier: str) -> None:
        """One build Job per model: refuse while one is active, otherwise delete
        a finished one so its name (kept for the TTL) doesn't block the relaunch."""
        try:
            previous = await self._appstore.job_status(identifier)
        except Exception as e:
            raise RuntimeError(f"Failed to check for an existing envbuild job: {e}") from e
        if previous is None:
            return
        if previous.status not in ("succeeded", "failed"):
            raise BuildInProgressError(
                f"A build is already {previous.status} for this model (job {previous.name})"
            )
        try:
            await self._appstore.delete_job(identifier)
        except Exception as e:
            raise RuntimeError(f"Failed to delete the previous envbuild job: {e}") from e
        logger.info(f"Deleted finished envbuild job {previous.name} before relaunch")

    def _check_files(self, model_rel: str, annotation_rel: str) -> None:
        """Fail fast on a missing model dir or annotation instead of letting the
        agent spend LLM budget on a job that can't get past L1."""
        model_dir = Path(self._settings.irods_mount_path) / model_rel
        if not model_dir.is_dir():
            raise ValueError(f"Model directory not found on the iRODS PVC: {model_rel}")
        execution_yaml = model_dir / annotation_rel / "execution.yaml"
        if not execution_yaml.is_file():
            raise ValueError(
                f"Annotation not found: {model_rel}/{annotation_rel}/execution.yaml"
            )


def _relative_subpath(subpath: str) -> str:
    """Normalise a caller-supplied path that must stay under the model dir."""
    rel = posixpath.normpath(subpath.replace("\\", "/"))
    if subpath.startswith("/") or rel in ("", ".") or rel == ".." or rel.startswith("../"):
        raise ValueError(
            f"annotation_subpath {subpath!r} must be a path inside the model directory"
        )
    return rel
