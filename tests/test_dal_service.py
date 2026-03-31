"""Tests for the DAL service layer."""

from __future__ import annotations

from mism_registry import ExecutionType, RunStatus

from services.dal_service import DALService


class TestDALService:
    def test_register_model(self, dal: DALService) -> None:
        model = dal.register_model(
            name="test-model",
            location_uri="irods:///mism/models/test",
            execution_type=ExecutionType.DOCKER,
            execution_ref="docker.io/org/model:v1",
        )
        assert model.id
        assert model.name == "test-model"
        assert model.execution_ref == "docker.io/org/model:v1"

    def test_register_model_with_metadata(self, dal: DALService) -> None:
        model = dal.register_model(
            name="gpu-model",
            location_uri="irods:///mism/models/gpu",
            execution_ref="docker.io/org/gpu:v1",
            metadata={"resource_requirements": {"cpus": "4", "memory": "8Gi"}},
        )
        assert model.metadata["resource_requirements"]["cpus"] == "4"

    def test_get_resource(self, dal: DALService) -> None:
        model = dal.register_model(
            name="find-me",
            location_uri="irods:///mism/models/find",
            execution_ref="docker.io/org/find:v1",
        )
        found = dal.get_resource(model.id)
        assert found is not None
        assert found.name == "find-me"

    def test_get_resource_not_found(self, dal: DALService) -> None:
        assert dal.get_resource("nonexistent") is None

    def test_run_lifecycle(self, dal: DALService) -> None:
        model = dal.register_model(
            name="test-model",
            location_uri="irods:///mism/models/test",
            execution_ref="docker.io/org/model:v1",
        )
        run = dal.create_run(model_id=model.id, notes="sid-123")
        assert run.id
        assert run.status == RunStatus.REGISTERED
        assert run.notes == "sid-123"

        run = dal.mark_running(run.id)
        assert run.status == RunStatus.RUNNING

        run = dal.mark_succeeded(run.id)
        assert run.status == RunStatus.COMPLETED

    def test_run_failure(self, dal: DALService) -> None:
        model = dal.register_model(
            name="fail-model",
            location_uri="irods:///mism/models/fail",
            execution_ref="docker.io/org/fail:v1",
        )
        run = dal.create_run(model_id=model.id)
        run = dal.mark_running(run.id)
        run = dal.mark_failed(run.id, "OOM killed")
        assert run.status == RunStatus.FAILED
        assert run.error_message == "OOM killed"

    def test_run_cancel(self, dal: DALService) -> None:
        model = dal.register_model(
            name="cancel-model",
            location_uri="irods:///mism/models/cancel",
            execution_ref="docker.io/org/cancel:v1",
        )
        run = dal.create_run(model_id=model.id)
        run = dal.cancel(run.id)
        assert run.status == RunStatus.CANCELLED

    def test_get_run_not_found(self, dal: DALService) -> None:
        assert dal.get_run("nonexistent-id") is None

    def test_find_runs_for_model(self, dal: DALService) -> None:
        model = dal.register_model(
            name="multi-run-model",
            location_uri="irods:///mism/models/multi",
            execution_ref="docker.io/org/multi:v1",
        )
        dal.create_run(model_id=model.id)
        dal.create_run(model_id=model.id)
        runs = dal.find_runs_for_model(model.id)
        assert len(runs) == 2
