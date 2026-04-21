# MISM Execution Platform

Orchestrates model execution on Kubernetes for the MISM ecosystem. Provides a FastAPI REST API that manages the run lifecycle via the DAL (mism-registry) and delegates all K8s orchestration to the appstore.

## Architecture

```
Discovery Gateway  ──>  Execution Platform (FastAPI)
                              │
                    ┌─────────┼─────────┐
                    │         │         │
               RunService     │   AppstoreClient
                    │         │    ├── /jobs/       (batch)
                    ▼         │    └── /containers/ (interactive)
               DAL Service    │         │
             (mism-registry)  │    Appstore / Tycho
                    │         │         │
              Postgres        │    Kubernetes
```

**Layering:**

1. **Endpoints** (`api/v1/`) — Thin FastAPI handlers.
2. **Services** (`services/`) — Business logic. Coordinates DAL and appstore client.
3. **DAL** (`services/dal_service.py`) — Wraps mism-registry for run lifecycle.
4. **Appstore Client** (`services/appstore_client.py`) — HTTP client for appstore's job and container endpoints.
5. **Schemas** (`schemas/`) — Pydantic request/response models.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/runs` | Execute a batch run (K8s Job via appstore) |
| `GET` | `/api/v1/runs` | List all runs |
| `GET` | `/api/v1/runs/{run_id}` | Get a run (includes live status) |
| `POST` | `/api/v1/runs/{run_id}/interactive` | Launch interactive session (via appstore) |
| `GET` | `/api/v1/runs/{run_id}/files` | List output files |
| `GET` | `/api/v1/runs/{run_id}/files/{filename}` | Download an output file |
| `DELETE` | `/api/v1/runs/{run_id}` | Cancel a run and delete K8s resources |
| `GET` | `/healthz` | Health check |

## Setup

### Prerequisites

- Python 3.10+
- The [mism-registry](../metadata-schema) package

### Install

```bash
make install
```

### Run locally

```bash
make dev
```

The server starts at `http://localhost:8000`. API docs at `http://localhost:8000/docs`.

### Run tests

```bash
make test
```

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | _(none)_ | Postgres connection URL; omit for InMemoryRegistry |
| `IRODS_PVC_NAME` | `irods-data` | PVC name for iRODS-backed storage |
| `IRODS_MOUNT_PATH` | `/irods` | Where the PVC is mounted on this pod |
| `APPSTORE_URL` | `http://helx-appstore:8000` | Appstore API URL |
| `APPSTORE_USERNAME` | `admin` | Appstore auth username |
| `APPSTORE_PASSWORD` | `admin` | Appstore auth password |
| `AMBASSADOR_URL` | `https://mism-apps.apps.renci.org` | Ambassador ingress for interactive sessions |
| `DEBUG` | `false` | Enable debug logging |

## Usage

### Execute a batch run

The Discovery Gateway creates a Run record via `prepare_run()`, then triggers execution:

```bash
curl -X POST https://mism-exec.apps.renci.org/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"run_id": "<run-id-from-prepare-run>"}'
```

### Launch an interactive session

```bash
curl -X POST https://mism-exec.apps.renci.org/api/v1/runs/<run-id>/interactive
```

Returns a URL to the interactive container (e.g., Jupyter).

### List output files

```bash
curl https://mism-exec.apps.renci.org/api/v1/runs/<run-id>/files
```

### Download a file

```bash
curl -O https://mism-exec.apps.renci.org/api/v1/runs/<run-id>/files/results.json
```

## Project Structure

```
execution-platform/
├── src/
│   ├── main.py                      # FastAPI app factory
│   ├── dependencies.py              # Dependency injection
│   ├── api/v1/
│   │   └── runs.py                  # Run endpoints
│   ├── services/
│   │   ├── dal_service.py           # DAL service (wraps mism-registry)
│   │   ├── run_service.py           # Run business logic
│   │   └── appstore_client.py       # HTTP client for appstore
│   ├── core/
│   │   ├── settings.py              # Environment-based configuration
│   │   ├── errors.py                # Error hierarchy
│   │   └── logging.py               # Structured logging
│   ├── middleware/
│   │   └── request_context.py       # Request ID + timing middleware
│   └── schemas/
│       ├── types.py                 # Validated types
│       └── runs.py                  # Run request/response models
├── tests/
├── scripts/                         # Test scripts for batch + interactive flows
├── helm/mism-exec/                  # Helm chart
├── k8s/                             # Raw K8s manifests
├── docs/                            # Architecture documents
├── pyproject.toml
├── Makefile
├── Dockerfile
└── docker-compose.yml               # Local dev
```
