"""Shared test fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mism_registry import InMemoryRegistry, register_dataset

from core.settings import Settings, get_settings
from dependencies import get_compute, get_dal, get_run_service, get_vivarium_service
from main import create_app
from orchestration.stub import StubCompute
from services.dal_service import DALService
from services.run_service import RunService
from services.vivarium_service import VivariumService


@pytest.fixture
def settings() -> Settings:
    return Settings(namespace="test", database_url=None, stub_compute=True)


@pytest.fixture
def registry() -> InMemoryRegistry:
    return InMemoryRegistry()


@pytest.fixture
def dal(registry: InMemoryRegistry) -> DALService:
    return DALService(registry)


@pytest.fixture
def stub_compute() -> StubCompute:
    return StubCompute()


@pytest.fixture
def run_service(dal: DALService, stub_compute: StubCompute, settings: Settings) -> RunService:
    return RunService(dal=dal, compute=stub_compute, settings=settings)


@pytest.fixture
def vivarium_service(stub_compute: StubCompute, settings: Settings) -> VivariumService:
    return VivariumService(compute=stub_compute, settings=settings)


@pytest.fixture
def client(
    dal: DALService,
    stub_compute: StubCompute,
    run_service: RunService,
    vivarium_service: VivariumService,
    settings: Settings,
) -> TestClient:
    """TestClient with StubCompute and real InMemory DAL."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_dal] = lambda: dal
    app.dependency_overrides[get_compute] = lambda: stub_compute
    app.dependency_overrides[get_run_service] = lambda: run_service
    app.dependency_overrides[get_vivarium_service] = lambda: vivarium_service
    return TestClient(app)


def create_test_run(dal: DALService) -> str:
    """Helper: register a model + input dataset + create a Run, return run_id."""
    model = dal.register_model(
        name="test-model",
        location_uri="irods:///mism/models/spike-predictor",
        execution_ref="docker.io/org/model:v1",
        metadata={"resource_requirements": {"cpus": "2", "memory": "4Gi"}},
    )
    input_ds = register_dataset(
        dal._registry,
        name="test-dataset",
        location_uri="/mism/datasets/cohort-a/data.csv",
    )
    run = dal.create_run(
        model_id=model.id,
        input_resource_ids=[input_ds.id],
        triggered_by="test",
    )
    return run.id
