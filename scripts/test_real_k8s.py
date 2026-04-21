"""End-to-end test against real K8s cluster.

Seeds a run in local Postgres, then calls the execution platform API
to launch a real Job in the hpatel namespace.

Prerequisites:
  - Local Postgres running (docker, port 5433)
  - Execution platform running with:
      DATABASE_URL=postgresql+psycopg2://mism:mism@localhost:5433/mism
      STUB_COMPUTE=false
      NAMESPACE=hpatel
      IRODS_PVC_NAME=stdnfs
      SERVICE_ACCOUNT=default
"""

import time
import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

DB_URL = "postgresql+psycopg2://mism:mism@localhost:5433/mism"
API_BASE = "http://localhost:8000"

# --- Seed ---
registry, session = create_registry(DB_URL)

VIVARIUM_IMAGE = "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d"
NOTEBOOK_PATH = "/home/jovyan/notebooks/01_vivarium_getting_started.ipynb"

model = register_model(
    registry,
    name="vivarium-notebook",
    location_uri="/test/models/vivarium",
    execution_type=ExecutionType.DOCKER,
    execution_ref=VIVARIUM_IMAGE,
    metadata={
        "resource_requirements": {"cpus": "1", "memory": "2Gi"},
        "command": [
            "jupyter", "nbconvert",
            "--to", "notebook",
            "--execute",
            "--ExecutePreprocessor.timeout=600",
            "--output-dir=/output",
            NOTEBOOK_PATH,
        ],
    },
)
print(f"Model: {model.id}")

dataset = register_dataset(
    registry,
    name="vivarium-sample-notebook",
    location_uri="/test/datasets/vivarium",
)
print(f"Dataset: {dataset.id}")

run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[dataset.id],
    triggered_by="k8s-test",
)
print(f"Run: {run.id}")
session.commit()
session.close()

# --- Launch ---
print("\n--- Launching Job ---")
resp = httpx.post(f"{API_BASE}/api/v1/runs", json={"run_id": run.id})
print(f"POST /runs → {resp.status_code}: {resp.json()}")

if resp.status_code != 201:
    print("Launch failed, exiting.")
    exit(1)

# --- Poll status ---
print("\n--- Polling status (every 3s, max 60s) ---")
for i in range(20):
    time.sleep(3)
    resp = httpx.get(f"{API_BASE}/api/v1/runs/{run.id}")
    data = resp.json()
    status = data["status"]
    phase = data.get("phase")
    print(f"  [{i*3:2d}s] status={status}  phase={phase}")
    if status in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")
