# Combined Execution Contract Example Usage

## 1. Purpose

This document provides example API usage for the combined execution contract.
It is a companion to the main contract and focuses on concrete JSON requests,
responses, and execution flows.

The examples assume:

- JSON request and response bodies
- OIDC bearer access tokens
- the `/api/v1/runs` execution API
- a shared DAL containing `Resource` and `Run` objects
- a parallel execution manager that creates and maintains `sid`
- persistent `HelxApp` objects
- per-user `HelxUser` objects
- per-run `HelxInst` objects
- `sid` stored as an annotation on `HelxInst`

## 2. Authentication Example

All execution endpoints require a bearer access token.

### Request

```http
POST /api/v1/runs HTTP/1.1
Host: execution.example.org
Authorization: Bearer eyJhbGciOi...
Content-Type: application/json
```

### Notes

The execution service validates the token and derives:

- canonical identity from `iss` and `sub`
- an internal username
- a Kubernetes-safe lowercase username

That Kubernetes-safe username is then used to identify the `HelxUser`,
associate the `HelxInst`, and construct routes where needed.

## 3. Example DAL Inputs

Before launch, the caller or an upstream system has already created the
required DAL objects.

### 3.1 Example model resource

```json
{
  "id": "2f6f2a1c-9c9f-4e7e-8b95-90fbb6d00a01",
  "name": "jupyter-sklearn-model",
  "resource_type": "model",
  "location_uri": "irods://mism/models/jupyter-sklearn-model",
  "execution_type": "docker",
  "execution_ref": "containers.example.org/mism/jupyter-sklearn:latest",
  "status": "active",
  "metadata": {
    "resource_requirements": {
      "cpus": "2",
      "memory": "4Gi"
    }
  }
}
```

### 3.2 Example dataset resources

```json
[
  {
    "id": "ec5236ad-3f3a-414f-92aa-f01c230d1001",
    "name": "training-data",
    "resource_type": "dataset",
    "location_uri": "irods://mism/data/training",
    "status": "active"
  },
  {
    "id": "fa533c41-57f9-4d74-9c8a-7c77c9ef1002",
    "name": "config-data",
    "resource_type": "dataset",
    "location_uri": "irods://mism/data/config",
    "status": "active"
  }
]
```

### 3.3 Example run record

```json
{
  "id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
  "model_id": "2f6f2a1c-9c9f-4e7e-8b95-90fbb6d00a01",
  "input_resource_ids": [
    "ec5236ad-3f3a-414f-92aa-f01c230d1001",
    "fa533c41-57f9-4d74-9c8a-7c77c9ef1002"
  ],
  "status": "registered",
  "parameters": {
    "task": "train",
    "epochs": 5
  },
  "environment": {
    "cpu": "2",
    "memory": "4Gi",
    "env_vars": {
      "EXPERIMENT_NAME": "baseline-run"
    }
  },
  "triggered_by": "portal"
}
```

## 4. Example Launch Flow

The external caller launches the run by referencing the existing `run_id`.

### 4.1 Launch request

```http
POST /api/v1/runs HTTP/1.1
Host: execution.example.org
Authorization: Bearer eyJhbGciOi...
Content-Type: application/json

{
  "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001"
}
```

### 4.2 Launch success response

```json
{
  "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
  "sid": "s-7fa2a89d",
  "status": "launching",
  "phase": null,
  "is_ready": false,
  "url": "https://apps.example.org/private/jupyterlab/alice9f2a3c4d5e6f/s-7fa2a89d/",
  "error": null
}
```

### 4.3 What the platform does internally

A successful launch typically performs the following internal steps:

1. validate the bearer token
2. resolve `iss` and `sub`
3. derive a Kubernetes-safe username
4. fetch the DAL `Run`
5. fetch the DAL model `Resource`
6. fetch the DAL dataset `Resource` objects
7. ensure a persistent `HelxApp` exists for the model
8. ensure a `HelxUser` exists for the caller
9. create a `sid`
10. create or update a `HelxInst`
11. store `sid` as a `HelxInst` annotation
12. populate run-specific mount configuration
13. allow helxapp-controller to reconcile the derived Kubernetes objects

## 5. Example HeLx Object Materialization

The following examples show the shape of the objects created or reused by the
execution manager.

### 5.1 Example HelxUser

```yaml
apiVersion: helx.renci.org/v1
kind: HelxUser
metadata:
  name: alice9f2a3c4d5e6f
spec:
  userHandle: "https://identity.example.org/users/alice9f2a3c4d5e6f"
  environment:
    OIDC_SUBJECT: "00u12abc-XYZ_9"
```

### 5.2 Example persistent HelxApp

```yaml
apiVersion: helx.renci.org/v1
kind: HelxApp
metadata:
  name: jupyter-sklearn-model
spec:
  appClassName: JupyterLab
  services:
    - name: main
      image: containers.example.org/mism/jupyter-sklearn:latest
      environment:
        MODEL_ID: "2f6f2a1c-9c9f-4e7e-8b95-90fbb6d00a01"
        INPUT_PATH: "/input"
        OUTPUT_PATH: "/output"
      ports:
        - containerPort: 8888
          port: 8888
      ambassador:
        prefix: "/private/jupyterlab/{{ .system.UserName }}/{{ .system.UUID }}/"
```

### 5.3 Example HelxInst

```yaml
apiVersion: helx.renci.org/v1
kind: HelxInst
metadata:
  name: run-99b8f64d-cbd8-4f7e-9e33-4a184ec87001
  annotations:
    mism.renci.org/sid: "s-7fa2a89d"
spec:
  appName: jupyter-sklearn-model
  userName: alice9f2a3c4d5e6f
  environment:
    RUN_ID: "99b8f64d-cbd8-4f7e-9e33-4a184ec87001"
    MODEL_ID: "2f6f2a1c-9c9f-4e7e-8b95-90fbb6d00a01"
    INPUT_PATH: "/input"
    OUTPUT_PATH: "/output"
    EXPERIMENT_NAME: "baseline-run"
    TASK: "train"
    EPOCHS: "5"
  resources:
    main:
      request:
        cpu: "2"
        memory: "4Gi"
      limit:
        cpu: "2"
        memory: "4Gi"
```

### 5.4 Example note on run-specific mounts

The current contract expects run-specific mounts to be driven at the
`HelxInst` layer. One likely realization is to extend the HeLx templates so
that `HelxInst` can contribute volume and mount data for input and output
paths.

An illustrative conceptual shape is:

```json
{
  "mounts": {
    "input-0": {
      "source_uri": "irods://mism/data/training",
      "mount_path": "/input/0",
      "read_only": true
    },
    "input-1": {
      "source_uri": "irods://mism/data/config",
      "mount_path": "/input/1",
      "read_only": true
    },
    "output": {
      "source_uri": "irods://mism/outputs/99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
      "mount_path": "/output",
      "read_only": false
    }
  }
}
```

This is only an example shape. The exact field placement remains to be
determined by the HeLx template and CRD extension design.

## 6. Example Status Polling

After launch, the caller polls the run endpoint for status.

### 6.1 Status request

```http
GET /api/v1/runs/99b8f64d-cbd8-4f7e-9e33-4a184ec87001 HTTP/1.1
Host: execution.example.org
Authorization: Bearer eyJhbGciOi...
```

### 6.2 Status response while reconciling

```json
{
  "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
  "sid": "s-7fa2a89d",
  "status": "launching",
  "phase": "Pending",
  "is_ready": false,
  "url": "https://apps.example.org/private/jupyterlab/alice9f2a3c4d5e6f/s-7fa2a89d/",
  "error": null
}
```

### 6.3 Status response when ready

```json
{
  "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
  "sid": "s-7fa2a89d",
  "status": "running",
  "phase": "Running",
  "is_ready": true,
  "url": "https://apps.example.org/private/jupyterlab/alice9f2a3c4d5e6f/s-7fa2a89d/",
  "error": null
}
```

### 6.4 Status response for batch completion

```json
{
  "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
  "sid": "s-7fa2a89d",
  "status": "completed",
  "phase": "Succeeded",
  "is_ready": null,
  "url": null,
  "error": null
}
```

## 7. Example Run Listing

### Request

```http
GET /api/v1/runs HTTP/1.1
Host: execution.example.org
Authorization: Bearer eyJhbGciOi...
```

### Response

```json
{
  "runs": [
    {
      "run_id": "99b8f64d-cbd8-4f7e-9e33-4a184ec87001",
      "sid": "s-7fa2a89d",
      "status": "running",
      "phase": "Running",
      "is_ready": true,
      "url": "https://apps.example.org/private/jupyterlab/alice9f2a3c4d5e6f/s-7fa2a89d/"
    },
    {
      "run_id": "ef14fbd9-80d7-470b-b284-532f6ef19002",
      "sid": "s-a2b390de",
      "status": "completed",
      "phase": "Succeeded",
      "is_ready": null,
      "url": null
    }
  ]
}
```

## 8. Example Cancellation

### 8.1 Cancel request

```http
DELETE /api/v1/runs/99b8f64d-cbd8-4f7e-9e33-4a184ec87001 HTTP/1.1
Host: execution.example.org
Authorization: Bearer eyJhbGciOi...
```

### 8.2 Cancel response

```http
HTTP/1.1 204 No Content
```

### 8.3 Expected platform behavior

On cancellation, the platform should:

1. mark the DAL run as cancelled
2. delete or deactivate the `HelxInst`
3. allow helxapp-controller to delete the derived `Deployment`, `Service`,
   and non-retained PVCs
4. invalidate or retire any user-facing routing as needed

## 9. Example Error Responses

### 9.1 Missing token

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{
  "error": "unauthorized",
  "message": "Missing, invalid, or expired bearer token."
}
```

### 9.2 Run not found

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{
  "error": "not_found",
  "message": "Run 99b8f64d-cbd8-4f7e-9e33-4a184ec87001 was not found."
}
```

### 9.3 Validation error

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{
  "error": "validation_error",
  "message": "Model resource is missing execution_ref."
}
```

### 9.4 Orchestration error

```http
HTTP/1.1 502 Bad Gateway
Content-Type: application/json

{
  "error": "orchestration_error",
  "message": "Failed to reconcile HelxInst into running Kubernetes resources."
}
```

## 10. Example Interactive Client Flow

A browser-based or API-based client can follow this flow:

1. obtain an OIDC bearer access token
2. create or reference model and dataset resources in the DAL
3. create or reference a `Run` in the DAL
4. `POST /api/v1/runs`
5. poll `GET /api/v1/runs/{run_id}` until `is_ready = true`
6. open the returned `url`

## 11. Example Batch Client Flow

A batch-oriented client can follow this flow:

1. obtain an OIDC bearer access token
2. create or reference model and dataset resources in the DAL
3. create or reference a `Run` in the DAL
4. `POST /api/v1/runs`
5. poll `GET /api/v1/runs/{run_id}` until `status` is `completed`,
   `failed`, or `cancelled`
6. retrieve outputs from the run's output storage path

## 12. Example Correlation Table

The following table shows how identifiers relate across layers.

| Layer | Example identifier | Purpose |
|------|--------------------|---------|
| OIDC identity | (`iss`, `sub`) | Canonical caller identity |
| Internal username | `00u12abcXYZ98f3a1c4d` | Stable application identity |
| Kubernetes-safe username | `00u12abc-xyz-9-8f3a1c4d2e6b` | Resource-safe identity |
| DAL run | `99b8f64d-cbd8-4f7e-9e33-4a184ec87001` | Run correlation |
| `sid` | `s-7fa2a89d` | Execution-manager-owned launch/session ID |
| `HelxInst` | `run-99b8f64d-cbd8-4f7e-9e33-4a184ec87001` | Concrete execution trigger |
| Deployment | generated by controller | Underlying workload |
| URL | `/private/jupyterlab/...` | User-facing route |

## 13. Example Annotation Propagation Expectation

The current contract expects the execution manager to store `sid` as a
`HelxInst` annotation, for example:

```yaml
metadata:
  annotations:
    mism.renci.org/sid: "s-7fa2a89d"
```

A planned controller extension should allow selected `HelxInst` annotations to
be propagated onto derived objects such as:

- `Deployment`
- `Service`
- other controller-generated resources as appropriate

This propagation enables easier traceability from Kubernetes objects back to
the originating run and execution record.

## 14. Notes

These examples are illustrative and are intended to show the expected shape of
requests, responses, and derived object relationships. They do not freeze the
exact internal CRD extension model for mounts or annotation propagation.