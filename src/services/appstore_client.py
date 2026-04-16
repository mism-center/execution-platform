"""Appstore client — calls the appstore API to launch interactive sessions."""

from __future__ import annotations

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


class AppstoreClient:
    """HTTP client for the appstore /api/v1/containers/ endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.appstore_url.rstrip("/")
        self._auth = (settings.appstore_username, settings.appstore_password)

    def launch(
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

        resp = httpx.post(
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

    def delete(self, sid: str) -> None:
        """Terminate an interactive session."""
        resp = httpx.delete(
            f"{self._base_url}/api/v1/containers/{sid}/",
            auth=self._auth,
            timeout=10.0,
        )
        if resp.status_code == 404:
            logger.warning(f"Session {sid} not found in appstore")
            return
        resp.raise_for_status()
        logger.info(f"Interactive session terminated: sid={sid}")
