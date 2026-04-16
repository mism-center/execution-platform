"""End-to-end test against the deployed mism-exec on mism-test."""

import time
import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

DB_URL = "postgresql+psycopg2://mism:changeme@localhost:5434/mism"
API_BASE = "https://mism-exec.apps.renci.org"

VIVARIUM_IMAGE = "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d"
NOTEBOOK_PATH = "/home/jovyan/notebooks/01_vivarium_getting_started.ipynb"

registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="vivarium-deployed",
    location_uri="/test/models/vivarium-deployed",
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
    name="vivarium-deployed-input",
    location_uri="/test/datasets/vivarium-deployed",
)
print(f"Dataset: {dataset.id}")

run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[dataset.id],
    triggered_by="deployed-test",
)
print(f"Run: {run.id}")
session.commit()
session.close()

print(f"\n--- Launching via {API_BASE} ---")
resp = httpx.post(f"{API_BASE}/api/v1/runs", json={"run_id": run.id})
print(f"POST /runs → {resp.status_code}: {resp.json()}")

if resp.status_code != 201:
    print("Launch failed.")
    exit(1)

print("\n--- Polling (every 10s, max 10min) ---")
for i in range(60):
    time.sleep(10)
    resp = httpx.get(f"{API_BASE}/api/v1/runs/{run.id}")
    data = resp.json()
    print(f"  [{(i+1)*10:3d}s] status={data['status']}  phase={data.get('phase')}")
    if data["status"] in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")
