# MISM Execution Platform

Orchestrates model execution on Kubernetes for the MISM ecosystem. Provides a FastAPI REST API that the Discovery Gateway invokes to launch containerized model runs, track their lifecycle via the DAL (mism-registry), and capture outputs.

## Architecture

```
Discovery Gateway  ──>  Execution Platform (FastAPI)
                              │
                    ┌─────────┼─────────┐
                    │         │         │
               RunService     │   VivariumService
                    │         │         │
                    ▼         │         ▼
               DAL Service    │    Compute (Protocol)
             (mism-registry)  │    ├── KubernetesCompute
                    │         │    └── StubCompute
              InMemory /      │         │
              Postgres        │    Deployment + Service
                              │    + Ambassador mapping
                              │
                         Kubernetes
```

**Layering:**

1. **Endpoints** (`api/v1/`) — Thin FastAPI handlers. Parse request, call service, return response.
2. **Services** (`services/`) — Business logic. Coordinates DAL and Compute to fulfil use cases.
3. **DAL** (`dal/`) — Wraps mism-registry for run lifecycle (register, running, completed, failed, cancelled).
4. **Orchestration** (`orchestration/`) — `Compute` protocol with Kubernetes and stub implementations. Typed result dataclasses (`StartResult`, `SystemStatus`).
5. **Schemas** (`schemas/`) — Pydantic request/response models with enums (`RunStatus`, `PodPhase`, `VivariumStatus`).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/runs` | Create a model execution run |
| `GET` | `/api/v1/runs` | List all runs |
| `GET` | `/api/v1/runs/{run_id}` | Get a run (includes live K8s status if active) |
| `DELETE` | `/api/v1/runs/{run_id}` | Cancel a run and delete K8s resources |
| `POST` | `/api/v1/poc/vivarium` | Launch Jupyter + vivarium-core |
| `GET` | `/api/v1/poc/vivarium/{sid}` | Get Vivarium instance status + access URL |
| `DELETE` | `/api/v1/poc/vivarium/{sid}` | Terminate a Vivarium instance |
| `GET` | `/healthz` | Health check |

### Status enums

**Run status:** `registered` | `running` | `completed` | `failed` | `cancelled`

**Pod phase:** `pending` | `running` | `succeeded` | `failed` | `unknown`

**Vivarium status:** `starting` | `ready` | `failed` | `unknown`

## Setup

### Prerequisites

- Python 3.10+
- Access to a Kubernetes cluster with Ambassador deployed (or set `STUB_COMPUTE=true` for local dev)
- The [mism-registry](../metadata-schema) package

### Install

```bash
make install
```

This installs the execution platform and the mism-registry library in editable mode.

### Run locally (stub compute — no K8s needed)

```bash
STUB_COMPUTE=true make dev
```

### Run locally (against a real K8s cluster)

```bash
NAMESPACE=<your-namespace> make dev
```

The server starts at `http://localhost:8000`. Interactive API docs at `http://localhost:8000/docs`.

### Run tests

```bash
make test
```

Tests use `StubCompute` and `InMemoryRegistry` — no cluster or database needed.

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NAMESPACE` | `hpatel` | K8s namespace for pods |
| `SERVICE_ACCOUNT` | `default` | K8s service account |
| `AMBASSADOR_ENABLED` | `true` | Use Ambassador routing for interactive pods |
| `STUB_COMPUTE` | `false` | Use in-memory stub instead of K8s |
| `DATABASE_URL` | _(none)_ | Postgres connection URL; omit for InMemoryRegistry |
| `IRODS_PVC_NAME` | `irods-data` | PVC name for iRODS-backed storage |
| `VIVARIUM_IMAGE` | `containers.renci.org/mism/vivarium-jupyter:latest` | Docker image for Vivarium PoC |
| `AUTH_ENABLED` | `false` | Enable OIDC authentication (not yet implemented) |
| `DEBUG` | `false` | Enable debug logging |

## Usage

### Create a model execution run

```bash
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "spike-predictor",
    "model_image": "containers.renci.org/mism/spike-predictor:v1",
    "input_data_path": "/mism/datasets/cohort-a/sequences.fasta",
    "cpus": "2",
    "memory": "4Gi"
  }'
```

### Get run details

```bash
curl http://localhost:8000/api/v1/runs/{run_id}
```

### List all runs

```bash
curl http://localhost:8000/api/v1/runs
```

### Cancel / delete a run

```bash
curl -X DELETE http://localhost:8000/api/v1/runs/{run_id}
```

### Launch Vivarium PoC

```bash
curl -X POST http://localhost:8000/api/v1/poc/vivarium \
  -H "Content-Type: application/json" \
  -d '{"cpus": "2", "memory": "4Gi"}'
```

The response includes a `url` with a Jupyter token — open it in a browser.

### Get Vivarium instance

```bash
curl http://localhost:8000/api/v1/poc/vivarium/{sid}
```

### Terminate Vivarium instance

```bash
curl -X DELETE http://localhost:8000/api/v1/poc/vivarium/{sid}
```

## Vivarium PoC Docker Image

The `docker/vivarium/` directory contains:

- `Dockerfile` — Extends `jupyter/scipy-notebook` with `vivarium-core` pre-installed
- `notebooks/01_vivarium_getting_started.ipynb` — Sample notebook demonstrating vivarium Process, Engine, and Composer

### Build and push

```bash
cd docker/vivarium
docker build -t <your-registry>/vivarium-jupyter:latest .
docker push <your-registry>/vivarium-jupyter:latest
```

Then set `VIVARIUM_IMAGE` to your pushed image tag.

## Project Structure

```
execution-platform/
├── src/
│   ├── main.py                      # FastAPI app factory + lifespan
│   ├── dependencies.py              # Dependency injection wiring
│   ├── api/v1/
│   │   ├── runs.py                  # Run endpoints (thin handlers)
│   │   └── poc.py                   # Vivarium PoC endpoints
│   ├── services/
│   │   ├── dal_service.py           # DAL service (wraps mism-registry)
│   │   ├── run_service.py           # Run business logic
│   │   └── vivarium_service.py      # Vivarium business logic
│   ├── core/
│   │   ├── settings.py              # Environment-based configuration
│   │   ├── errors.py                # Error hierarchy + exception handlers
│   │   └── logging.py              # Structured logging + request ID filter
│   ├── middleware/
│   │   └── request_context.py       # x-request-id + timing middleware
│   ├── orchestration/
│   │   ├── compute.py               # Compute protocol + typed results
│   │   ├── kube.py                  # Kubernetes implementation
│   │   ├── stub.py                  # Stub implementation for local dev
│   │   ├── models.py                # SystemSpec, ContainerSpec, etc.
│   │   └── templates/
│   │       ├── pod.yaml             # Pod manifest template (Jinja2)
│   │       └── service.yaml         # Service + Ambassador mapping template
│   └── schemas/
│       ├── enums.py                 # RunStatus, PodPhase, VivariumStatus
│       ├── types.py                 # NonEmptyStr, ImageRef, DataPath, K8sQuantity
│       ├── runs.py                  # Run request/response models
│       └── poc.py                   # Vivarium request/response models
├── tests/
│   ├── conftest.py                  # Shared fixtures
│   ├── test_dal_service.py          # DAL unit tests
│   ├── test_services.py             # Service layer tests
│   ├── test_runs_api.py             # Run endpoint + validation tests
│   ├── test_poc_api.py              # Vivarium endpoint tests
│   └── test_orchestration.py        # Model, protocol, and stub tests
├── docker/vivarium/
│   ├── Dockerfile
│   └── notebooks/
├── docs/                            # Architecture design documents
├── pyproject.toml
├── Makefile
├── .env.example
└── README.md
```

## Contributing

### Adding a new compute backend

Implement the `Compute` protocol defined in `orchestration/compute.py`:

```python
class Compute(Protocol):
    def start(self, system: SystemSpec) -> StartResult: ...
    def status(self, sid: str) -> SystemStatus | None: ...
    def delete(self, sid: str) -> None: ...
```

Then register it in `dependencies.py`.

### Switching to Postgres

Set `DATABASE_URL` to a Postgres connection string. The DAL service factory automatically selects the Postgres backend. No code changes needed.
