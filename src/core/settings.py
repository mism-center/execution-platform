"""Application settings — all config via environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Execution platform configuration.

    Every field can be overridden by an environment variable with the same
    (upper-case) name, e.g. ``NAMESPACE=hpatel``.
    """

    # --- General ---
    env: Literal["local", "dev", "val", "prod"] = "local"
    debug: bool = False

    # --- Kubernetes ---
    namespace: str = "hpatel"
    service_account: str = "default"
    stub_compute: bool = False  # True → use StubCompute instead of K8s

    # --- DAL / Registry ---
    database_url: str | None = None  # None → InMemoryRegistry

    # --- iRODS / Storage ---
    irods_pvc_name: str = "irods-data"
    # Where the iRODS PVC is mounted on the execution platform pod itself
    # (used for listing / downloading run output files).
    irods_mount_path: str = "/irods"

    # --- Appstore (interactive sessions) ---
    appstore_url: str = "http://helx-appstore:8000"
    appstore_username: str = "admin"
    appstore_password: str = "admin"
    ambassador_url: str = "https://mism-apps.apps.renci.org"

    # --- Vivarium PoC ---
    vivarium_image: str = "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d"
    poc_output_pvc: str = "stdnfs"
    poc_output_base_dir: str = "/mism/poc/outputs"
    poc_notebook_path: str = "/home/jovyan/notebooks/01_vivarium_getting_started.ipynb"

    # --- Auth (not enforced yet — MISM-181) ---
    auth_enabled: bool = False
    auth_mode: Literal["jwt", "oidc"] = "oidc"
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None

    # --- GPU (skipped for PoC) ---
    gpu_resource_name: str = "nvidia.com/gpu"

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
