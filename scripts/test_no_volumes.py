"""Minimal test — no volume mounts, just confirm Job runs and exits."""

import time
import httpx
from mism_registry import register_model, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

DB_URL = "postgresql+psycopg2://mism:mism@localhost:5433/mism"
API_BASE = "http://localhost:8000"

registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="busybox-no-vol",
    location_uri="/test/models/busybox-novol",
    execution_type=ExecutionType.DOCKER,
    execution_ref="busybox:latest",
    metadata={
        "resource_requirements": {"cpus": "0.1", "memory": "64Mi"},
        "command": ["/bin/sh", "-c", "echo hello && sleep 2 && echo done"],
    },
)
print(f"Model: {model.id}")

# No input datasets — so no input volumes
run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[],
    triggered_by="k8s-test-novol",
)
print(f"Run: {run.id}")
session.commit()
session.close()

print("\n--- Launching Job (no volumes) ---")
resp = httpx.post(f"{API_BASE}/api/v1/runs", json={"run_id": run.id})
print(f"POST /runs → {resp.status_code}: {resp.json()}")

if resp.status_code != 201:
    print("Launch failed.")
    exit(1)

print("\n--- Polling (every 3s, max 60s) ---")
for i in range(20):
    time.sleep(3)
    resp = httpx.get(f"{API_BASE}/api/v1/runs/{run.id}")
    data = resp.json()
    print(f"  [{(i+1)*3:2d}s] status={data['status']}  phase={data.get('phase')}")
    if data["status"] in ("completed", "failed", "cancelled"):
        break

print(f"\nFinal: {data}")
