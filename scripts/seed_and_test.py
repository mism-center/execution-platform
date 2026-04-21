"""Seed local Postgres with test data and exercise the /runs API."""

import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

DB_URL = "postgresql+psycopg2://mism:mism@localhost:5433/mism"
API_BASE = "http://localhost:8000"

# --- Seed ---
registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="test-model",
    location_uri="/mism/models/spike-predictor",
    execution_type=ExecutionType.DOCKER,
    execution_ref="docker.io/library/alpine:latest",
    metadata={"resource_requirements": {"cpus": "1", "memory": "2Gi"}},
)
print(f"Model registered: {model.id}")

dataset = register_dataset(
    registry,
    name="test-dataset",
    location_uri="/mism/datasets/cohort-a/data.csv",
)
print(f"Dataset registered: {dataset.id}")

run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[dataset.id],
    triggered_by="seed-script",
)
print(f"Run created: {run.id} (status={run.status})")

session.commit()
session.close()

# --- Test API ---
print("\n--- Testing API ---")

# POST /api/v1/runs
resp = httpx.post(f"{API_BASE}/api/v1/runs", json={"run_id": run.id})
print(f"POST /runs → {resp.status_code}: {resp.json()}")

# GET /api/v1/runs/{run_id}
resp = httpx.get(f"{API_BASE}/api/v1/runs/{run.id}")
print(f"GET  /runs/{run.id} → {resp.status_code}: {resp.json()}")

# GET /api/v1/runs
resp = httpx.get(f"{API_BASE}/api/v1/runs")
print(f"GET  /runs → {resp.status_code}: {resp.json()}")
