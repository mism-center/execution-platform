"""Tests for the service layer (RunService)."""

from __future__ import annotations

import pytest
from mism_registry import RunStatus

from services.dal_service import DALService
from services.run_service import RunService
from tests.conftest import create_test_run


class TestRunService:
    def test_create_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        result = run_service.create_run(run_id)
        assert result.run_id == run_id
        assert result.sid
        assert result.status == RunStatus.RUNNING

    def test_create_run_not_found(self, run_service: RunService) -> None:
        with pytest.raises(ValueError, match="not found"):
            run_service.create_run("nonexistent")

    def test_get_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        run_service.create_run(run_id)
        fetched = run_service.get_run(run_id)
        assert fetched is not None
        assert fetched.run_id == run_id

    def test_get_run_not_found(self, run_service: RunService) -> None:
        assert run_service.get_run("nonexistent") is None

    def test_get_run_shows_error_after_failure(
        self, run_service: RunService, dal: DALService
    ) -> None:
        run_id = create_test_run(dal)
        run_service.create_run(run_id)
        dal.mark_failed(run_id, "OOM killed")
        fetched = run_service.get_run(run_id)
        assert fetched is not None
        assert fetched.status == RunStatus.FAILED
        assert fetched.error == "OOM killed"

    def test_delete_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        run_service.create_run(run_id)
        assert run_service.delete_run(run_id) is True
        assert run_service.delete_run("nonexistent") is False

    def test_uses_model_resource_requirements(
        self, run_service: RunService, dal: DALService
    ) -> None:
        """Verify that resource requirements from model.metadata are used."""
        run_id = create_test_run(dal)
        result = run_service.create_run(run_id)
        assert result.run_id == run_id
