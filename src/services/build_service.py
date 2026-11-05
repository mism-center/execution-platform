"""Build service — fires the envagent image builder (envbuild) for a model.

ExecAPI asks appstore for one K8s Job shaped like envagent's
``deploy/agent-job.yaml``; the agent creates its own Kaniko and verification
pods (as the ``envbuild`` ServiceAccount) and writes its verdict to the
envbuild-work PVC.

Builds are keyed by the model's resource id (appstore identifier
``build-<resource_id>``), so there is at most one build Job per model. The DAL's
``env_build_status`` (MISM-296) tracks it: discovery sets READY_FOR_BUILD,
``launch`` moves it to BUILDING once the Job exists, and the poller reports the
outcome. ExecAPI does not set RUNNABLE/BUILD_FAILED itself — the outcome goes
to a reporter (discovery's internal build callback), which owns the terminal
transition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
import uuid
from functools import partial
from pathlib import Path

from mism_registry import EnvBuildStatus, ResourceRegistrationStatus, ResourceType

from core.settings import Settings
from core.storage import pvc_subpath
from schemas.builds import BuildRequest, BuildResponse, BuildStatusResponse
from services.appstore_client import AppstoreClient
from services.build_reporter import BuildReporter, BuildResult, LoggingBuildReporter
from services.dal_service import DALService

logger = logging.getLogger(__name__)

# Mount points inside the agent pod, fixed by envagent's Job template.
MODELS_MOUNT = "/models"
WORK_MOUNT = "/work"
DOCKER_CONFIG_MOUNT = "/home/node/.docker"  # harness image runs as user `node`
# envagent's record dir, relative to the envbuild-work PVC root. The agent sees
# it at WORK_MOUNT/RECORDS_DIR; ExecAPI at settings.envbuild_work_mount_path/RECORDS_DIR.
RECORDS_DIR = "records"

# Prefix keeps the build's mism-guid label distinct from the annotation Job's,
# which is the bare resource_id: appstore's job_status/delete_job select by
# that label, and delete_job removes every match.
BUILD_IDENTIFIER_PREFIX = "build-"
JOB_NAME_PREFIX = "envbuild"

# The Job is named "envbuild-<run_id>-build-<resource_id>" (appstore builds
# f"{name}-{identifier}"). Carrying run_id in the name is how the poller finds
# this build's verdict row (run.run_id) — appstore's job status exposes nothing
# else per launch. The name must be a DNS label (<= 63 chars), which caps the
# resource id at 37 chars (a uuid is 36).
RUN_ID_LEN = 10
_RESOURCE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,35}[a-z0-9])?$")

_ACTIVE_JOB_STATES = ("pending", "running")


class BuildInProgressError(Exception):
    """A build for this model is already underway."""


def build_identifier(resource_id: str) -> str:
    return f"{BUILD_IDENTIFIER_PREFIX}{resource_id}"


def _job_name_prefix(run_id: str) -> str:
    return f"{JOB_NAME_PREFIX}-{run_id}"


def run_id_from_job_name(job_name: str, resource_id: str) -> str | None:
    """Recover the run_id from ``envbuild-<run_id>-build-<resource_id>``."""
    m = re.fullmatch(
        rf"{JOB_NAME_PREFIX}-([0-9a-f]{{{RUN_ID_LEN}}})-{re.escape(build_identifier(resource_id))}",
        job_name,
    )
    return m.group(1) if m else None


class BuildService:
    """Launches envbuild agent Jobs through appstore and reports their outcome."""

    def __init__(
        self,
        dal: DALService,
        appstore: AppstoreClient,
        settings: Settings,
        reporter: BuildReporter | None = None,
    ) -> None:
        self._dal = dal
        self._appstore = appstore
        self._settings = settings
        self._reporter = reporter or LoggingBuildReporter()
        # Process-local: resource_id -> name of the Job last handed to the
        # reporter, so a model still BUILDING (callback not yet applied) isn't
        # re-reported every poll — nor reported again as "missing" once the
        # Job's TTL removes it. A restart re-reports once; the callback must be
        # idempotent.
        self._reported: dict[str, str] = {}
        # job_name -> monotonic time the poller first saw it finished without a
        # verdict on the PVC (absorbs RWX file-cache lag before giving up).
        self._verdict_wait_started: dict[str, float] = {}

    @staticmethod
    async def _in_executor(fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    async def launch(self, request: BuildRequest) -> BuildResponse:
        """Validate the model, create the builder Job, then mark it BUILDING.

        Raises LookupError (unknown resource), ValueError (not buildable),
        BuildInProgressError (a build for this model is already underway) or
        RuntimeError (appstore or the DAL failed).
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
        if resource.env_build_status == EnvBuildStatus.BUILDING:
            raise BuildInProgressError(f"A build is already underway for {resource.id}")
        # Discovery queues the model by setting READY_FOR_BUILD (approval does it
        # automatically; retry/rebuild do it explicitly).
        if resource.env_build_status != EnvBuildStatus.READY_FOR_BUILD:
            raise ValueError(
                f"Resource {request.resource_id} has env_build_status="
                f"{resource.env_build_status.value}, expected ready_for_build"
            )

        model_rel = pvc_subpath(resource.location_uri, self._settings.irods_mount_path)
        annotation_rel = _relative_subpath(request.annotation_subpath)
        await self._in_executor(self._check_files, model_rel, annotation_rel)

        identifier = build_identifier(resource.id)
        await self._clear_previous_build(identifier)

        run_id = uuid.uuid4().hex[:RUN_ID_LEN]
        model_repo = f"{MODELS_MOUNT}/{model_rel}"
        image = self._settings.envbuild_agent_image

        env = {
            "MODEL_ID": f"mism:model/{resource.id}",
            "MODEL_REPO": model_repo,
            "ANNOTATION": f"{model_repo}/{annotation_rel}",
            "ENVBUILD_OUTPUTS": f"{WORK_MOUNT}/{RECORDS_DIR}",
            "ENVBUILD_WORK_PVC": self._settings.envbuild_work_pvc,
            "ENVBUILD_WORK_MOUNT": WORK_MOUNT,
            "ENVBUILD_MODELS_PVC": self._settings.irods_pvc_name,
            "ENVBUILD_MODELS_MOUNT": MODELS_MOUNT,
            # Stamped on every attempt/verdict row as run.run_id; the poller
            # matches on it (also carried in the Job name).
            "ENVBUILD_RUN_ID": run_id,
            "ENVBUILD_HARNESS_IMAGE": image,
        }
        if request.prompt:
            env["PROMPT"] = request.prompt

        try:
            result = await self._appstore.launch_job(
                name=_job_name_prefix(run_id),
                identifier=identifier,
                image=image,
                cpus=self._settings.envbuild_cpus,
                memory=self._settings.envbuild_memory,
                env=env,
                # Whole-claim mounts omit sub_path: appstore defaults it to ""
                # but rejects an explicit blank value (DRF allow_blank=False).
                pvc_mounts=[
                    {
                        "pvc": self._settings.envbuild_work_pvc,
                        "mount_path": WORK_MOUNT,
                        "read_only": False,
                    },
                    {
                        "pvc": self._settings.irods_pvc_name,
                        "mount_path": MODELS_MOUNT,
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

        # Mark BUILDING only once the Job exists: there is no BUILDING ->
        # READY_FOR_BUILD transition to undo a failed launch with.
        try:
            await self._in_executor(
                self._dal.set_env_build_status, resource.id, EnvBuildStatus.BUILDING
            )
        except Exception as e:
            # Don't leave an agent running that the DAL doesn't know about.
            try:
                await self._appstore.delete_job(identifier)
            except Exception:
                logger.warning(
                    f"envbuild job {result.name} launched but could not be deleted after the "
                    "BUILDING transition failed — it may be orphaned",
                    exc_info=True,
                )
            raise RuntimeError(
                f"envbuild job launched but marking {resource.id} BUILDING failed: {e}"
            ) from e

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
        a finished one so it doesn't linger next to the new launch."""
        try:
            previous = await self._appstore.job_status(identifier)
        except Exception as e:
            raise RuntimeError(f"Failed to check for an existing envbuild job: {e}") from e
        if previous is None:
            return
        if previous.status in _ACTIVE_JOB_STATES:
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

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------

    async def poll_building_resources(self) -> None:
        """Report the outcome of every BUILDING model whose Job has finished."""
        resources = await self._in_executor(
            self._dal.list_resources_by_env_build_status, EnvBuildStatus.BUILDING
        )
        for resource in resources:
            try:
                await self._sync_build(resource.id)
            except Exception:
                logger.warning(
                    f"Poller: failed to sync envbuild for resource {resource.id}",
                    exc_info=True,
                )

    async def _sync_build(self, resource_id: str) -> None:
        identifier = build_identifier(resource_id)
        status = await self._appstore.job_status(identifier)

        if status is None:
            if resource_id in self._reported:
                return  # its finished Job was reported; the TTL has since removed it
            # Deleted, or removed by the TTL before anyone recorded an outcome.
            await self._report(BuildResult(
                resource_id=resource_id, run_id=None, job_name=identifier,
                job_status="missing", exit_code=None,
            ))
            return
        if status.status in _ACTIVE_JOB_STATES:
            return
        if self._reported.get(resource_id) == status.name:
            return

        run_id = run_id_from_job_name(status.name, resource_id)
        gathered = None
        if run_id:
            gathered = await self._in_executor(self._read_outputs, resource_id, run_id)

        if gathered is None and status.status == "succeeded":
            # A clean exit always writes a verdict; give the RWX mount time to
            # surface it before reporting the build as verdict-less.
            first = self._verdict_wait_started.setdefault(status.name, time.monotonic())
            if time.monotonic() - first < self._settings.envbuild_verdict_grace_seconds:
                return

        self._verdict_wait_started.pop(status.name, None)
        await self._report(BuildResult(
            resource_id=resource_id,
            run_id=run_id,
            job_name=status.name,
            job_status=status.status,
            exit_code=status.exit_code,
            **(gathered or {}),
        ))

    async def _report(self, result: BuildResult) -> None:
        if self._reported.get(result.resource_id) == result.job_name:
            return
        # Raises on a retryable delivery failure: nothing is recorded, so the
        # next poll tries again.
        await self._reporter.report(result)
        self._reported[result.resource_id] = result.job_name

    def _read_outputs(self, resource_id: str, run_id: str) -> dict | None:
        """This build's verdict row plus what the callback needs from the PVC:
        attempt rows, the run contract from state.json, the best attempt's
        Dockerfile/lockfile, and any annotation patch. None if the verdict
        isn't there (yet)."""
        records = Path(self._settings.envbuild_work_mount_path) / RECORDS_DIR
        model_id = f"mism:model/{resource_id}"
        verdict = None
        for row in reversed(_read_jsonl(records / "verdicts.jsonl")):
            run = row.get("run") or {}
            if row.get("model_id") == model_id and run.get("run_id") == run_id:
                verdict = row
                break
        if verdict is None:
            return None

        job_id = verdict.get("job_id") or ""
        attempts = [
            row for row in _read_jsonl(records / "attempts.jsonl")
            if row.get("job_id") == job_id
        ]
        job_dir = records / "jobs" / job_id
        if not job_id or not job_dir.is_dir():
            return {"verdict": verdict, "attempts": attempts}

        # Paths relative to the envbuild-work PVC root.
        files = sorted(
            f"{RECORDS_DIR}/jobs/{job_id}/{p.relative_to(job_dir).as_posix()}"
            for p in job_dir.rglob("*") if p.is_file()
        )

        state = _read_json(job_dir / "state.json") or {}
        spec = (state.get("best") or {}).get("spec") or state.get("spec")

        # The best attempt is the one that produced the verdict's image.
        best_attempt = None
        digest = verdict.get("image_digest")
        if digest:
            for row in attempts:
                if row.get("image_digest") == digest:
                    best_attempt = row.get("attempt")
        dockerfile = lockfile = None
        if best_attempt is not None:
            dockerfile = _read_text(job_dir / f"Dockerfile.a{best_attempt}")
            lockfile = _read_text(job_dir / f"lockfile.a{best_attempt}.txt")

        return {
            "verdict": verdict,
            "attempts": attempts,
            "files": files,
            "spec": spec,
            "dockerfile": dockerfile,
            "lockfile": lockfile,
            "annotation_patch": _read_text(job_dir / "annotation-patch.yaml"),
        }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _read_json(path: Path) -> dict | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSONL file; missing file or malformed lines are skipped."""
    try:
        text = path.read_text()
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _relative_subpath(subpath: str) -> str:
    """Normalise a caller-supplied path that must stay under the model dir."""
    rel = posixpath.normpath(subpath.replace("\\", "/"))
    if subpath.startswith("/") or rel in ("", ".") or rel == ".." or rel.startswith("../"):
        raise ValueError(
            f"annotation_subpath {subpath!r} must be a path inside the model directory"
        )
    return rel
