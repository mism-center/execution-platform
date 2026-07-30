"""Tests for the bounded retry helper used by /runs/{id}/files and
/runs/{id}/files/{filename} to absorb the iRODS PVC cross-pod visibility lag
(mirrors the equivalent fix in model-discovery's
RegistryService._metadata_package_dir)."""

from __future__ import annotations

from api.v1.runs import _exists_with_retry
from core.settings import Settings


async def test_succeeds_on_first_check() -> None:
    settings = Settings(
        database_url=None, run_files_retry_max_attempts=3, run_files_retry_backoff_seconds=0.0
    )
    assert await _exists_with_retry(lambda: True, settings) is True


async def test_succeeds_after_transient_misses() -> None:
    settings = Settings(
        database_url=None, run_files_retry_max_attempts=3, run_files_retry_backoff_seconds=0.0
    )
    calls = {"count": 0}

    def flaky_check() -> bool:
        calls["count"] += 1
        return calls["count"] >= 3

    assert await _exists_with_retry(flaky_check, settings) is True
    assert calls["count"] == 3


async def test_gives_up_after_max_attempts() -> None:
    settings = Settings(
        database_url=None, run_files_retry_max_attempts=2, run_files_retry_backoff_seconds=0.0
    )
    calls = {"count": 0}

    def always_missing() -> bool:
        calls["count"] += 1
        return False

    assert await _exists_with_retry(always_missing, settings) is False
    assert calls["count"] == 2
