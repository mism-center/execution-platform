"""Where finished envbuild outcomes go.

The poller hands every finished build to a ``BuildReporter``. In deployment
that is ``HttpBuildReporter``, which POSTs the result to discovery's internal
build callback (``/api/internal/builds``); discovery owns the env_build_status
terminal transition (RUNNABLE / BUILD_FAILED) and records the image.
``LoggingBuildReporter`` is the fallback when no callback URL is configured.

Delivery contract with discovery: 2xx = accepted, 4xx = rejected for good (not
retried), 5xx / timeout / connection error = retried on the next poll. The
callback must be idempotent — a restarted ExecAPI re-sends builds that are
still BUILDING.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildResult:
    """Everything known about one finished build Job."""

    resource_id: str
    run_id: str | None  # None when the Job name couldn't be matched to a launch
    job_name: str
    job_status: str  # succeeded | failed | missing (Job gone before an outcome was recorded)
    exit_code: int | None
    # envagent's verdict row (records/verdicts.jsonl); None if it never wrote one.
    verdict: dict | None = None
    # envagent's attempt rows for this build (records/attempts.jsonl).
    attempts: list[dict] = field(default_factory=list)
    # The verified run contract from the agent's state (best.spec): mount,
    # install_mode, env_vars, entrypoint. None when there is no state.json.
    spec: dict | None = None
    # Contents of the best attempt's Dockerfile / lockfile, and the proposed
    # annotation corrections (only written when a correction passed the ladder).
    dockerfile: str | None = None
    lockfile: str | None = None
    annotation_patch: str | None = None
    # Per-job files on the envbuild-work PVC, relative to its root (for logs).
    files: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """verified | failed | escalated | budget_exhausted | error | no_verdict | job_missing"""
        if self.verdict and self.verdict.get("status"):
            return self.verdict["status"]
        return "job_missing" if self.job_status == "missing" else "no_verdict"

    @property
    def image_ref(self) -> str | None:
        return (self.verdict or {}).get("image_ref")

    @property
    def image_digest(self) -> str | None:
        return (self.verdict or {}).get("image_digest")

    @property
    def reason(self) -> str:
        if self.verdict:
            return self.verdict.get("reason") or ""
        if self.job_status == "missing":
            return "build job disappeared before its outcome was recorded"
        return f"agent exited without a verdict (job {self.job_status}, exit code {self.exit_code})"


def build_callback_payload(result: BuildResult) -> dict[str, Any]:
    """The body agreed with discovery for POST /api/internal/builds."""
    verdict = result.verdict or {}
    spec = result.spec or {}

    # What L3 actually ran beats the spec's declared entrypoint.
    entrypoint: list[str] | None = None
    if verdict.get("entrypoint_used"):
        entrypoint = shlex.split(verdict["entrypoint_used"])
    elif spec.get("entrypoint"):
        entrypoint = list(spec["entrypoint"])

    return {
        "resource_id": result.resource_id,
        "run_id": result.run_id or "",
        "job": {
            "name": result.job_name,
            "status": result.job_status,
            "exit_code": result.exit_code,
        },
        "outcome": result.outcome,
        "image": (
            {"ref": result.image_ref, "digest": result.image_digest or ""}
            if result.image_ref else None
        ),
        "ladder_reached": verdict.get("ladder_reached") or "",
        "reason": result.reason,
        "failure_class": verdict.get("failure_class"),
        "routes_to": verdict.get("routes_to"),
        "runtime": {
            "entrypoint": entrypoint or [],
            "install_mode": spec.get("install_mode"),
            "mount": spec.get("mount"),
            "env_vars": spec.get("env_vars") or {},
        },
        "artifacts": {
            "dockerfile": result.dockerfile,
            "lockfile": result.lockfile,
            "lockfile_sha256": verdict.get("lockfile_sha256"),
            "annotation_patch": result.annotation_patch,
        },
        # ``action`` is the repair applied after that attempt failed.
        "attempts": [
            {
                "attempt": row.get("attempt"),
                "ladder_reached": row.get("ladder_reached"),
                "failure_class": row.get("failure_class"),
                "action": (
                    {"type": row["action_taken"].get("type"), "arg": row["action_taken"].get("arg")}
                    if isinstance(row.get("action_taken"), dict) else None
                ),
                "duration_s": row.get("duration_s"),
            }
            for row in result.attempts
        ],
        "verdict": result.verdict or {},
    }


class BuildReporter(Protocol):
    async def report(self, result: BuildResult) -> None:
        """Deliver one finished build. Raise to have the poller retry it."""
        ...


class LoggingBuildReporter:
    """Fallback when no callback URL is configured."""

    async def report(self, result: BuildResult) -> None:
        logger.info(
            f"envbuild finished (no callback configured): resource={result.resource_id} "
            f"run_id={result.run_id} job={result.job_name} job_status={result.job_status} "
            f"outcome={result.outcome} image={result.image_ref} "
            f"files={len(result.files)} reason={result.reason!r}"
        )


class HttpBuildReporter:
    """POSTs the result to discovery's internal build callback."""

    def __init__(self, url: str, timeout_seconds: float = 30.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def report(self, result: BuildResult) -> None:
        payload = build_callback_payload(result)
        # Connection errors and timeouts propagate: the poller retries next tick.
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._url, json=payload, timeout=self._timeout)

        if resp.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"build callback returned {resp.status_code} for {result.resource_id} "
                f"(run {result.run_id}); body={resp.text[:500]!r} — will retry",
                request=resp.request,
                response=resp,
            )
        if resp.status_code == 409:
            # Model no longer in a build state — typically a replay after an
            # ExecAPI restart of a result discovery already applied.
            logger.info(
                f"build callback 409 for {result.resource_id} (run {result.run_id}): "
                f"already applied or not building; not retrying. body={resp.text[:300]!r}"
            )
            return
        if resp.status_code >= 400:
            # Rejected for good: retrying the same payload can't succeed.
            logger.error(
                f"build callback rejected {result.resource_id} (run {result.run_id}) with "
                f"{resp.status_code}; not retrying. body={resp.text[:500]!r}"
            )
            return
        logger.info(
            f"build callback accepted: resource={result.resource_id} run_id={result.run_id} "
            f"outcome={result.outcome} status={resp.status_code}"
        )
