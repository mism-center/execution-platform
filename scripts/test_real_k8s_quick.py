"""Quick end-to-end test with busybox — runs in seconds."""

import time
import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

DB_URL = "postgresql+psycopg2://mism:changeme@localhost:5434/mism"
API_BASE = "http://localhost:8000"

# --- Seed ---
registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="busybox-test",
    location_uri="/test/models/busybox",
    execution_type=ExecutionType.DOCKER,
    execution_ref="busybox:latest",
    metadata={
        "resource_requirements": {"cpus": "0.1", "memory": "64Mi"},
        "command": [
            "/bin/sh", "-c",
            "echo 'Hello from MISM' > /output/result.txt && "
            "echo MODEL_ID=$MODEL_ID >> /output/result.txt && "
            "echo RUN_ID=$RUN_ID >> /output/result.txt && "
            "echo 'Done'",
        ],
    },
)
print(f"Model: {model.id}")

dataset = register_dataset(
    registry,
    name="dummy-input",
    location_uri="/test/datasets/dummy",
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
    print("Launch failed.")
    exit(1)

# --- Poll ---
print("\n--- Polling (every 3s, max 60s) ---")
for i in range(20):
    time.sleep(3)
    resp = httpx.get(f"{API_BASE}/api/v1/runs/{run.id}")
    data = resp.json()
    print(f"  [{(i+1)*3:2d}s] status={data['status']}  phase={data.get('phase')}")
    if data["status"] in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")
