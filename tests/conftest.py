"""Shared test fixtures."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from mism_registry import InMemoryRegistry, register_dataset

from core.settings import Settings, get_settings
from dependencies import get_dal, get_run_service
from main import create_app
from services.appstore_client import AppstoreClient, JobResult, JobStatus
from services.dal_service import DALService
from services.run_service import RunService


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=None)


@pytest.fixture
def registry() -> InMemoryRegistry:
    return InMemoryRegistry()


@pytest.fixture
def dal(registry: InMemoryRegistry) -> DALService:
    return DALService(registry)


@pytest.fixture
def mock_appstore() -> AppstoreClient:
    """Mock appstore client that returns fake job results."""
    client = MagicMock(spec=AppstoreClient)
    client.launch_job.return_value = JobResult(
        sid="fake-sid", name="fake-job", status="running"
    )
    client.job_status.return_value = JobStatus(
        sid="fake-sid", name="fake-job", status="running", phase="running"
    )
    client.delete_job.return_value = None
    client.delete_container.return_value = None
    return client


@pytest.fixture
def run_service(dal: DALService, mock_appstore: AppstoreClient, settings: Settings) -> RunService:
    return RunService(dal=dal, appstore=mock_appstore, settings=settings)


@pytest.fixture
def client(
    dal: DALService,
    mock_appstore: AppstoreClient,
    run_service: RunService,
    settings: Settings,
) -> TestClient:
    """TestClient with mock appstore and real InMemory DAL."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_dal] = lambda: dal
    app.dependency_overrides[get_run_service] = lambda: run_service
    return TestClient(app)


def create_test_run(dal: DALService, registry: InMemoryRegistry | None = None) -> str:
    """Helper: register a model + input dataset + create a Run, return run_id."""
    model = dal.register_model(
        name="test-model",
        location_uri="irods:///mism/models/spike-predictor",
        execution_ref="docker.io/org/model:v1",
        metadata={"resource_requirements": {"cpus": "2", "memory": "4Gi"}},
    )
    reg = registry or dal._in_memory
    input_ds = register_dataset(
        reg,
        name="test-dataset",
        location_uri="/mism/datasets/cohort-a/data.csv",
    )
    run = dal.create_run(
        model_id=model.id,
        input_resource_ids=[input_ds.id],
        triggered_by="test",
    )
    return run.id
