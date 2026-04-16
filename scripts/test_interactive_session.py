"""Interactive session flow: register → launch via execution platform → surface URL.

1. Register model + dataset in DAL
2. Create a Run via prepare_run()
3. Launch interactive session via POST /runs/{run_id}/interactive
   (execution platform calls appstore internally)
4. Surface the accessible URL
"""

import httpx
from mism_registry import register_model, register_dataset, prepare_run, ExecutionType
from mism_registry.backends.postgres import create_registry

# --- Config ---
DB_URL = "postgresql+psycopg2://mism:changeme@localhost:5434/mism"
EXEC_API = "https://mism-exec.apps.renci.org"
# EXEC_API = "http://localhost:8000"  # local testing
VIVARIUM_IMAGE = "helxplatform/vivarium-jupyter@sha256:c2bda6bbddea091ed4aa96f1fa3b6b41f51ad234d432c2412dd4919b76c77f6d"

# === Step 1: Register resources in DAL ===
print("=== Step 1: Register model + dataset ===")
registry, session = create_registry(DB_URL)

model = register_model(
    registry,
    name="vivarium-interactive",
    location_uri="/models/vivarium-interactive",
    execution_type=ExecutionType.DOCKER,
    execution_ref=VIVARIUM_IMAGE,
    metadata={
        "resource_requirements": {"cpus": "1", "memory": "2Gi"},
    },
)
print(f"  Model: {model.id}")

dataset = register_dataset(
    registry,
    name="vivarium-explore-data",
    location_uri="/datasets/vivarium-explore",
)
print(f"  Dataset: {dataset.id}")

# === Step 2: Create Run ===
print("\n=== Step 2: Create Run ===")
run = prepare_run(
    registry,
    model_id=model.id,
    input_resource_ids=[dataset.id],
    triggered_by="interactive-test",
)
print(f"  Run: {run.id} (status={run.status})")
session.commit()
session.close()

# === Step 3: Launch interactive session via execution platform ===
print(f"\n=== Step 3: POST /api/v1/runs/{run.id}/interactive ===")
resp = httpx.post(f"{EXEC_API}/api/v1/runs/{run.id}/interactive", follow_redirects=True, timeout=120.0)
print(f"  Status: {resp.status_code}")
try:
    data = resp.json()
    print(f"  Body: {data}")
except Exception:
    print(f"  Raw: {resp.text[:500]}")
    exit(1)

if resp.status_code == 201:
    print(f"\n=== Interactive session launched ===")
    print(f"  SID: {data['sid']}")
    print(f"  URL: {data['url']}")
    print(f"  Input data mounted at /data/input")
    print(f"  Output (writable) at /data/output")
else:
    print("  Launch failed.")
