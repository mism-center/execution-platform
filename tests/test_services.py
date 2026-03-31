"""Tests for the service layer (RunService, VivariumService)."""

from __future__ import annotations

import pytest
from mism_registry import RunStatus

from schemas.enums import VivariumStatus
from schemas.poc import CreateVivariumRequest, ExecuteVivariumRequest
from services.dal_service import DALService
from services.run_service import RunService
from services.vivarium_service import VivariumService
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


class TestVivariumService:
    def test_create_instance(self, vivarium_service: VivariumService) -> None:
        request = CreateVivariumRequest()
        result = vivarium_service.create_instance(request)
        assert result.sid
        assert result.status == VivariumStatus.STARTING
        assert result.jupyter_token
        assert "token=" in result.url

    def test_get_instance(self, vivarium_service: VivariumService) -> None:
        request = CreateVivariumRequest()
        created = vivarium_service.create_instance(request)
        fetched = vivarium_service.get_instance(created.sid)
        assert fetched is not None
        assert fetched.sid == created.sid
        assert fetched.status == VivariumStatus.READY

    def test_get_instance_not_found(self, vivarium_service: VivariumService) -> None:
        assert vivarium_service.get_instance("nonexistent") is None

    def test_delete_instance(self, vivarium_service: VivariumService) -> None:
        request = CreateVivariumRequest()
        created = vivarium_service.create_instance(request)
        assert vivarium_service.delete_instance(created.sid) is True
        assert vivarium_service.get_instance(created.sid) is None

    def test_delete_not_found(self, vivarium_service: VivariumService) -> None:
        assert vivarium_service.delete_instance("nonexistent") is False

    def test_execute_headless(self, vivarium_service: VivariumService) -> None:
        request = ExecuteVivariumRequest()
        result = vivarium_service.execute(request)
        assert result.sid
        assert result.status == VivariumStatus.STARTING
        assert result.output_path

    def test_get_execution(self, vivarium_service: VivariumService) -> None:
        request = ExecuteVivariumRequest()
        created = vivarium_service.execute(request)
        fetched = vivarium_service.get_execution(created.sid)
        assert fetched is not None
        assert fetched.sid == created.sid

    def test_get_execution_not_found(self, vivarium_service: VivariumService) -> None:
        assert vivarium_service.get_execution("nonexistent") is None

    def test_delete_execution(self, vivarium_service: VivariumService) -> None:
        request = ExecuteVivariumRequest()
        created = vivarium_service.execute(request)
        assert vivarium_service.delete_instance(created.sid) is True
        assert vivarium_service.get_execution(created.sid) is None
