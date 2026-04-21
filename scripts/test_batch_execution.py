"""Batch execution flow: register → run → capture output on PVC.

1. Register model + dataset in DAL
2. Create a Run via prepare_run()
3. Trigger headless execution via POST /runs
4. Poll until completed
5. Verify output Resource registered in DAL
"""

import time
import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

# --- Config ---
DB_URL = "postgresql+psycopg2://mism:changeme@localhost:5434/mism"
EXEC_API = "https://mism-exec.apps.renci.org"
# EXEC_API = "http://localhost:8000"  # local testing
VIVARIUM_IMAGE = "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d"
NOTEBOOK_PATH = "/home/jovyan/notebooks/01_vivarium_getting_started.ipynb"

# === Step 1: Register resources in DAL ===
print("=== Step 1: Register model + dataset ===")
registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="vivarium-batch",
    location_uri="/models/vivarium-batch",
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
print(f"  Model: {model.id}")

dataset = register_dataset(
    registry,
    name="vivarium-input",
    location_uri="/datasets/vivarium-input",
)
print(f"  Dataset: {dataset.id}")

# === Step 2: Create Run ===
print("\n=== Step 2: Create Run ===")
run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[dataset.id],
    triggered_by="batch-test",
)
print(f"  Run: {run.id} (status={run.status})")
session.commit()
session.close()

# === Step 3: Trigger execution ===
print(f"\n=== Step 3: POST /api/v1/runs ===")
resp = httpx.post(f"{EXEC_API}/api/v1/runs", json={"run_id": run.id}, follow_redirects=True, timeout=120.0)
print(f"  Status: {resp.status_code}")
try:
    print(f"  Body: {resp.json()}")
except Exception:
    print(f"  Raw: {resp.text[:500]}")

if resp.status_code != 201:
    print("  Launch failed.")
    exit(1)

# === Step 4: Poll until complete ===
print("\n=== Step 4: Polling ===")
for i in range(60):
    time.sleep(10)
    resp = httpx.get(f"{EXEC_API}/api/v1/runs/{run.id}", timeout=60.0)
    data = resp.json()
    print(f"  [{(i+1)*10:3d}s] status={data['status']}  phase={data.get('phase')}")
    if data["status"] in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")

# === Step 5: Verify output Resource in DAL ===
if data["status"] == "completed":
    print("\n=== Step 5: Verify output Resource ===")
    registry2, session2 = create_registry(DB_URL)
    updated_run = registry2.get_run(run.id)
    print(f"  output_resource_ids: {updated_run.output_resource_ids}")
    if updated_run.output_resource_ids:
        output_res = registry2.get_resource(updated_run.output_resource_ids[0])
        print(f"  Output Resource: {output_res.id}")
        print(f"  location_uri: {output_res.location_uri}")
    session2.close()
