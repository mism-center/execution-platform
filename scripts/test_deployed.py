"""End-to-end test against the deployed mism-exec on mism-test."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_URL, EXEC_API, MODEL_IMAGE, NOTEBOOK_PATH

import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="vivarium-deployed",
    location_uri="/test/models/vivarium-deployed",
    execution_type=ExecutionType.DOCKER,
    execution_ref=MODEL_IMAGE,
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

print(f"\n--- Launching via {EXEC_API} ---")
resp = httpx.post(f"{EXEC_API}/api/v1/runs", json={"run_id": run.id})
print(f"POST /runs → {resp.status_code}: {resp.json()}")

if resp.status_code != 201:
    print("Launch failed.")
    exit(1)

print("\n--- Polling (every 10s, max 10min) ---")
for i in range(60):
    time.sleep(10)
    resp = httpx.get(f"{EXEC_API}/api/v1/runs/{run.id}")
    data = resp.json()
    print(f"  [{(i+1)*10:3d}s] status={data['status']}  phase={data.get('phase')}")
    if data["status"] in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")
