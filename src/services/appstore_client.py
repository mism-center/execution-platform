"""Appstore client — calls the appstore API for K8s orchestration.

Handles both interactive sessions (Deployments via /containers/) and
batch execution (Jobs via /jobs/).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from core.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveSession:
    """Result of launching an interactive container via appstore."""

    sid: str
    url: str
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class JobResult:
    """Result of launching a batch Job via appstore."""

    sid: str
    name: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class JobStatus:
    """Status of a batch Job."""

    sid: str
    name: str
    status: str  # running, succeeded, failed, pending
    phase: str
    exit_code: int | None = None


class AppstoreClient:
    """Async HTTP client for appstore orchestration endpoints."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.appstore_url.rstrip("/")
        self._auth = (settings.appstore_username, settings.appstore_password)
        self._delete_retry_max_attempts = settings.appstore_delete_retry_max_attempts
        self._delete_retry_backoff_seconds = settings.appstore_delete_retry_backoff_seconds

    # ------------------------------------------------------------------
    # Interactive sessions (/api/v1/containers/)
    # ------------------------------------------------------------------

    async def launch(
        self,
        *,
        image: str,
        name: str,
        port: int = 8888,
        cpus: float = 1.0,
        memory: str = "2G",
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        pvc_mounts: list[dict[str, Any]] | None = None,
    ) -> InteractiveSession:
        """Launch an interactive container and return the session info."""
        payload: dict[str, Any] = {
            "image": image,
            "name": name,
            "port": port,
            "cpus": cpus,
            "memory": memory,
            "env": env or {},
            "pvc_mounts": pvc_mounts or [],
        }
        if command:
            payload["command"] = command

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/v1/containers/",
                json=payload,
                auth=self._auth,
                timeout=30.0,
            )
        resp.raise_for_status()
        data = resp.json()

        logger.info(f"Interactive session launched: sid={data['sid']}")
        return InteractiveSession(
            sid=data["sid"],
            url=data["url"],
            name=data["name"],
        )

    async def delete_container(self, sid: str) -> None:
        """Terminate an interactive session."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base_url}/api/v1/containers/{sid}/",
                auth=self._auth,
                timeout=10.0,
            )
        if resp.status_code == 404:
            logger.warning(f"Container {sid} not found in appstore")
            return
        resp.raise_for_status()
        logger.info(f"Interactive session terminated: sid={sid}")

    # ------------------------------------------------------------------
    # Batch Jobs (/api/v1/jobs/)
    # ------------------------------------------------------------------

    async def launch_job(
        self,
        *,
        name: str,
        identifier: str,
        image: str,
        cpus: str = "1",
        memory: str = "2Gi",
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        pvc_mounts: list[dict[str, Any]] | None = None,
        username: str = "mism",
    ) -> JobResult:
        """Launch a batch K8s Job."""
        payload: dict[str, Any] = {
            "name": name,
            "identifier": identifier,
            "image": image,
            "cpus": cpus,
            "memory": memory,
            "env": env or {},
            "pvc_mounts": pvc_mounts or [],
            "username": username,
        }
        if command:
            payload["command"] = command

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/v1/jobs/",
                json=payload,
                auth=self._auth,
                timeout=30.0,
            )
        resp.raise_for_status()
        data = resp.json()

        logger.info(f"Batch job launched: sid={data['sid']}")
        return JobResult(
            sid=data["sid"],
            name=data["name"],
            status=data["status"],
        )

    async def job_status(self, sid: str) -> JobStatus | None:
        """Get status of a batch Job."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/v1/jobs/{sid}/",
                auth=self._auth,
                timeout=10.0,
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

        return JobStatus(
            sid=data["sid"],
            name=data["name"],
            status=data["status"],
            phase=data["phase"],
            exit_code=data.get("exit_code"),
        )

    async def delete_job(self, sid: str) -> None:
        """Delete a batch Job.

        appstore has been observed returning a transient 500 when a Job is
        deleted immediately after it reports a terminal status (plausibly a
        race with the Job's own K8s teardown). Retry a bounded number of
        times before giving up; on final failure, include the response body
        in the raised error so the real appstore-side cause is visible in
        logs instead of just httpx's generic "Server error" message.
        """
        last_resp: httpx.Response | None = None
        for attempt in range(1, self._delete_retry_max_attempts + 1):
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{self._base_url}/api/v1/jobs/{sid}/",
                    auth=self._auth,
                    timeout=10.0,
                )
            if resp.status_code == 404:
                logger.warning(f"Job {sid} not found in appstore")
                return
            if resp.status_code < 500:
                resp.raise_for_status()
                logger.info(f"Batch job deleted: sid={sid}")
                return

            last_resp = resp
            if attempt < self._delete_retry_max_attempts:
                logger.warning(
                    f"Delete job {sid} failed (attempt {attempt}/"
                    f"{self._delete_retry_max_attempts}): {resp.status_code} "
                    f"body={resp.text[:500]!r} — retrying"
                )
                await asyncio.sleep(self._delete_retry_backoff_seconds * attempt)

        assert last_resp is not None
        raise httpx.HTTPStatusError(
            f"Server error '{last_resp.status_code}' deleting job {sid} after "
            f"{self._delete_retry_max_attempts} attempts; "
            f"response body: {last_resp.text[:500]!r}",
            request=last_resp.request,
            response=last_resp,
        )
