"""Application settings — all config via environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Execution platform configuration.

    Every field can be overridden by an environment variable with the same
    (upper-case) name, e.g. ``APPSTORE_URL=http://helx-appstore:8000``.
    """

    # --- General ---
    env: Literal["local", "dev", "val", "prod"] = "local"
    debug: bool = False

    # --- DAL / Registry ---
    database_url: str | None = None  # None → InMemoryRegistry

    # --- iRODS / Storage ---
    irods_pvc_name: str = "irods-data"
    irods_mount_path: str = "/irods"  # where PVC is mounted on this pod

    # --- Appstore (K8s orchestration) ---
    appstore_url: str = "http://helx-appstore:8000"
    appstore_username: str = "admin"
    appstore_password: str = "admin"
    ambassador_url: str = "https://mism-apps.apps.renci.org"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached Settings instance. Call in tests for isolation."""
    get_settings.cache_clear()
