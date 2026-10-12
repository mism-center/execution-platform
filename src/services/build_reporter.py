"""Where finished envbuild outcomes go.

The poller hands every finished build to a ``BuildReporter``. The intended
implementation calls discovery's build-callback API (image location, verdict,
output files), and discovery owns the env_build_status terminal transition
(RUNNABLE / BUILD_FAILED). That API doesn't exist yet, so the default reporter
only logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

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
    # Per-job files on the envbuild-work PVC, relative to its root.
    files: list[str] = field(default_factory=list)

    @property
    def verdict_status(self) -> str | None:
        """verified | failed | escalated | budget_exhausted | error, or None."""
        return (self.verdict or {}).get("status")

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


class BuildReporter(Protocol):
    async def report(self, result: BuildResult) -> None:
        """Deliver one finished build. Must be idempotent: a restarted poller
        re-reports builds that are still BUILDING."""
        ...


class LoggingBuildReporter:
    """Placeholder until discovery's build-callback API exists."""

    async def report(self, result: BuildResult) -> None:
        logger.info(
            f"envbuild finished (callback not wired yet): resource={result.resource_id} "
            f"run_id={result.run_id} job={result.job_name} job_status={result.job_status} "
            f"verdict={result.verdict_status} image={result.image_ref} "
            f"files={len(result.files)} reason={result.reason!r}"
        )
