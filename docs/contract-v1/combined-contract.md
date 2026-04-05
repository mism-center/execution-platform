# Combined Execution Contract

## 1. Purpose

This document defines a combined execution contract that reconciles three
existing systems:

1. the discovery execution contract for model execution
2. AppStore as the user-facing and API-facing execution broker
3. helxapp-controller as the Kubernetes reconciliation layer

The purpose of this contract is to preserve all required capabilities of the
existing discovery execution contract while aligning execution with the HeLx
application model and the helxapp-controller data model.

This document assumes OIDC-based authentication and identity propagation as
already defined for the platform. OIDC provides the authenticated caller
identity. The platform derives a canonical internal username and a
Kubernetes-safe username from the token claims. The Kubernetes-safe username is
used anywhere Kubernetes naming constraints apply, including user-facing
routing, resource naming, and labels.

## 2. Scope

This contract covers:

- request and response semantics for launching executions
- mapping from API objects to AppStore and helxapp-controller objects
- mapping from API objects to the parallel execution manager
- run lifecycle and status reporting
- identity propagation into user and workload objects
- routing and launch URL generation
- cancellation semantics
- testing requirements
- assumptions, expectations, and to-be-determined items

This contract does not redefine the DAL schema itself. It defines how DAL
objects, AppStore objects, execution-manager objects, and Kubernetes objects
relate to one another.

## 3. Design Principles

The reconciled design follows these principles:

1. The combined contract MUST offer at least the features of the existing
   discovery execution contract.
2. The public execution API SHOULD remain centered on the existing run-based
   interface, so clients can continue to prepare a run and then trigger it.
3. AppStore SHOULD remain focused on interactive application access, routing,
   and the user-facing instance experience.
4. A parallel execution manager SHOULD own run orchestration concerns that are
   not native to AppStore, including run correlation, `sid` management, and
   run-specific execution materialization.
5. helxapp-controller SHOULD remain the Kubernetes control-plane component that
   renders and reconciles HelxApp, HelxUser, and HelxInst resources.
6. The tuple (`iss`, `sub`) MUST remain the canonical caller identity.
7. Any username used in Kubernetes object names or routes MUST be transformed
   into a Kubernetes-safe lowercase form.
8. `HelxApp` SHOULD represent a long-lived configuration collection point,
   rather than an ephemeral per-run artifact.

## 4. Existing Systems and Their Roles

### 4.1 Discovery execution contract

The existing discovery execution contract defines a shared-DAL model in which
the Discovery Portal creates model resources, dataset resources, and run
records, then triggers execution via `POST /api/v1/runs` using a `run_id`.
The execution platform reads the DAL, launches Kubernetes execution, and
returns run status, readiness, URL, and cancellation support.

The contract currently requires at minimum:

- a model resource with `execution_ref`
- zero or more dataset resources with `location_uri`
- a run record with parameters and environment
- launch via `POST /api/v1/runs`
- status via `GET /api/v1/runs/{run_id}`
- listing via `GET /api/v1/runs`
- cancellation via `DELETE /api/v1/runs/{run_id}`

### 4.2 AppStore

AppStore already provides an authenticated programmatic API for launching
applications. A client authenticates, AppStore launches an instance through
its API, the user polls readiness, and then accesses the app through an
Ambassador-routed URL. AppStore also owns the user-facing instance lifecycle,
including launch, readiness, enumeration, and termination.

AppStore does not natively model pipelines or batch collections. Therefore, in
this combined contract, AppStore SHOULD be treated as recognizing and serving
the launched batch-capable application components rather than the higher-level
execution collection as a whole.

### 4.3 helxapp-controller

helxapp-controller manages three CRDs:

- `HelxApp`: application template
- `HelxUser`: user record
- `HelxInst`: per-user instance request

When all three exist and reference each other, the controller synthesizes
Kubernetes Deployments, Services, and PersistentVolumeClaims. It also supports
environment merging, user-level volumes, pod security context resolution, and
Ambassador mapping generation.

### 4.4 Parallel execution manager

A separate execution-manager process is expected to run alongside AppStore and
to manipulate the same underlying HeLx objects. This execution manager SHOULD
be the component that:

- processes `POST /api/v1/runs`
- maps DAL runs into HeLx execution objects
- creates and maintains the canonical `sid`
- defines run-specific mounts and places them onto `HelxInst`
- correlates DAL `Run` records with `HelxInst`, controller UUIDs, and
  AppStore-visible instances
- updates run status using execution-layer and Kubernetes observations

AppStore MAY surface or consume the `sid`, but the execution manager is the
authoritative owner of it.

## 5. Reconciled Architecture

The reconciled architecture is:

```text
Discovery Gateway / client
    -> shared DAL (`Resource`, `Run`)
    -> combined execution API (`/api/v1/runs`)
    -> parallel execution manager
    -> HelxUser + HelxApp + HelxInst CRDs
    -> helxapp-controller
    -> Kubernetes objects (Deployment, Service, PVC)
    -> AppStore user-facing access path
    -> Ambassador route / launch URL
```

Under this model:

- the DAL remains the system of record for model, dataset, and run intent
- the `/api/v1/runs` API remains the stable launch interface
- the parallel execution manager becomes the authoritative run orchestrator
- AppStore becomes a consumer of the same HeLx objects for access, readiness,
  and user-facing interaction
- helxapp-controller remains the Kubernetes reconciler
- the returned `sid` is owned by the execution manager and SHOULD be surfaced
  as an annotation on `HelxInst`
- the returned `url` is the routed launch URL when the execution is web
  accessible

## 6. Core Object Model

### 6.1 DAL objects

The DAL remains responsible for the abstract execution request:

- `Resource` of type `model` or `tool`
- `Resource` of type `dataset`
- `Run`

The model resource provides `execution_ref`, execution metadata, and optional
resource requirements. Dataset resources provide input storage locations. The
run ties together model, inputs, parameters, and requested environment.

### 6.2 Execution-manager objects

The execution manager contributes:

- API handling for `/api/v1/runs`
- derivation of caller identity into platform execution identity
- `sid` creation and maintenance
- run-to-HeLx object correlation
- run-specific volume and mount materialization
- run lifecycle updates back into the DAL

### 6.3 AppStore objects

AppStore contributes:

- authenticated user/session context for user-facing interaction
- instance visibility and access semantics
- readiness polling and routed URL construction for interactive apps
- optional instance-scoped identity token

AppStore is not the authoritative owner of the overall run abstraction or the
`sid`, but it SHOULD interoperate with both when interactive access is needed.

### 6.4 helxapp-controller objects

helxapp-controller contributes the Kubernetes-facing execution objects:

- `HelxUser` for the authenticated caller
- `HelxApp` for the model or application template
- `HelxInst` for the concrete execution request

These objects are the authoritative bridge from the API request into concrete
Kubernetes resources.

### 6.5 Kubernetes objects

The underlying Kubernetes objects include:

- `Deployment`
- `Service`
- `PersistentVolumeClaim`
- Ambassador-derived route annotation on a Service, when applicable

These are derived outputs, not directly client-visible API resources.

## 7. Identity and User Mapping

### 7.1 Canonical identity

The canonical authenticated identity is the tuple:

- `iss`
- `sub`

The API MUST validate the bearer token before execution and MUST preserve
`iss` and `sub` for audit.

### 7.2 Internal username

The platform SHOULD derive an internal username from the validated token:

```text
internal_username = alnum(sub) + hex(sha256(iss))
```

This form is deterministic and stable for the same (`iss`, `sub`) pair.

### 7.3 Kubernetes-safe username

Because OIDC claims may contain uppercase or other characters that are not
safe for Kubernetes naming and routing, the platform MUST derive a second
form:

```text
k8s_username = dns_label(lowercase(sub)) + "-" + hex(sha256(iss))[:12]
```

The Kubernetes-safe username MUST:

- be lowercase
- contain only characters allowed by the target Kubernetes naming rule
- preserve cross-issuer uniqueness
- remain stable for the same (`iss`, `sub`) pair

### 7.4 Mapping into HeLx objects

The authenticated OIDC caller MUST map into platform user identity as follows:

- `HelxUser.metadata.name` SHOULD use the Kubernetes-safe username
- `HelxInst.spec.userName` MUST reference that `HelxUser`
- AppStore-visible username handling SHOULD resolve to the same effective
  lowercase username used in routing and labels
- any Ambassador route prefix MUST use the Kubernetes-safe username
- audit records MUST retain the original `iss` and `sub`

If AppStore maintains an internal Django `User`, that user SHOULD be keyed or
linked by the same derived username used for `HelxUser`.

## 8. Mapping DAL Runs to HeLx Execution Objects

### 8.1 Model resource to HelxApp

A DAL model resource maps to a persistent `HelxApp` template.

The minimum mapping is:

- `Resource.execution_ref` -> `HelxApp.spec.services[].image`
- `Resource.name` -> `HelxApp.metadata.name` or generated application name
- `Resource.metadata.resource_requirements` -> service resource bounds or
  default resource selections
- model-defined environment defaults -> `HelxApp.spec.services[].environment`

Because `HelxApp` is expected to be a long-term configuration collection
point, it SHOULD persist independently of individual runs.

When the model is web-accessible, the HelxApp service SHOULD define:

- an exposed port
- Ambassador mapping
- any required base-path environment such as `NB_PREFIX`

When the model is batch-only, the HelxApp MAY omit Ambassador configuration.

### 8.2 Run to HelxInst

A DAL `Run` maps to a `HelxInst`.

The minimum mapping is:

- `Run.id` -> execution correlation and labels
- `Run.parameters` -> environment variables or structured instance config
- `Run.environment` -> requested resources and env overrides
- `Run.triggered_by` -> audit metadata
- `Run.model_id` -> selected `HelxApp`
- execution-manager `sid` -> `HelxInst` annotation
- run-specific mounts -> `HelxInst` volume-related configuration

The `HelxInst` is the concrete trigger that causes helxapp-controller to
create Kubernetes resources.

The execution-manager-owned `sid` SHOULD be stored as an annotation on
`HelxInst`. A future extension to helxapp-controller SHOULD allow selected
annotations from `HelxInst` to be propagated onto derived resources such as
`Deployment` objects so that downstream systems can correlate Kubernetes
artifacts back to the originating execution.

### 8.3 Authenticated caller to HelxUser

The authenticated caller maps to one `HelxUser`.

The minimum mapping is:

- derived Kubernetes-safe username -> `HelxUser.metadata.name`
- user security context source -> `HelxUser.spec.userHandle`, if used
- user-level environment and volumes -> `HelxUser.spec.environment` and
  `HelxUser.spec.volumes`

If the platform already has a user identity service that can return POSIX
settings, `HelxUser.spec.userHandle` SHOULD point to it so the controller can
resolve `runAsUser`, `runAsGroup`, `fsGroup`, and
`supplementalGroups`.

### 8.4 Datasets and run mounts

Input dataset resources remain part of the DAL contract, but their storage
location MUST be reflected in the rendered workload.

At minimum, dataset `location_uri` values MUST be mounted into the launched
container such that the container contract remains compatible with the current
execution contract:

- `/input` for a single input
- `/input/0`, `/input/1`, ... for multiple inputs
- `/output` for run outputs

The execution manager is expected to define these run-specific mounts and place
them into `HelxInst` or into HeLx-rendered data derived from `HelxInst`.

A likely implementation path is to extend the volume or deployment template so
that `HelxInst` can directly drive the required run-specific mounts while still
remaining compatible with helxapp-controller's rendering model.

The client-visible container contract MUST preserve `INPUT_PATH` and
`OUTPUT_PATH`.

## 9. API Contract

### 9.1 Authentication

Launch endpoints MUST be protected with OIDC-issued OAuth 2.0 bearer access
tokens.

Clients MUST send:

```http
Authorization: Bearer <access_token>
```

The API MUST validate token signature, issuer, audience, expiration, and any
required permissions before execution.

### 9.2 Trigger execution

The public launch interface remains:

```http
POST /api/v1/runs
Content-Type: application/json
Authorization: Bearer <access_token>

{
  "run_id": "<uuid>"
}
```

The combined execution service MUST:

1. validate the bearer token
2. fetch the `Run` from the DAL
3. fetch the model `Resource`
4. fetch input dataset `Resource` objects
5. derive `internal_username` and `k8s_username`
6. ensure the corresponding `HelxUser` exists
7. ensure a compatible persistent `HelxApp` exists for the model
8. create or update a `HelxInst` that binds the user, app, parameters,
   resources, mounts, and execution metadata
9. create and maintain the canonical `sid`
10. make that `sid` visible through a `HelxInst` annotation and API
    responses
11. return a run response compatible with the discovery execution contract

### 9.3 Launch response

The response MUST preserve the existing minimum shape:

```json
{
  "run_id": "abc-123",
  "sid": "a3f9c2",
  "status": "running",
  "phase": null,
  "is_ready": false,
  "url": "https://.../private/<app>/<username>/...",
  "error": null
}
```

Response semantics:

- `run_id`: the DAL run identifier
- `sid`: the execution-manager-owned instance or session identifier
- `status`: run lifecycle status
- `phase`: live Kubernetes or instance phase, when known
- `is_ready`: whether the workload is ready for access
- `url`: launch URL when the workload is web-accessible; otherwise `null`
- `error`: structured error detail, when present

### 9.4 Query run status

```http
GET /api/v1/runs/{run_id}
Authorization: Bearer <access_token>
```

The status endpoint MUST combine:

- DAL run lifecycle state
- execution-manager run state
- AppStore readiness or access state, when applicable
- Kubernetes readiness or phase from the underlying objects

A successful response SHOULD include:

- `run_id`
- `sid`
- `status`
- `phase`
- `is_ready`
- `url`
- `error`

### 9.5 List runs

```http
GET /api/v1/runs
Authorization: Bearer <access_token>
```

The list endpoint SHOULD return runs visible to the caller and SHOULD be
filtered by the authenticated user identity.

### 9.6 Cancel run

```http
DELETE /api/v1/runs/{run_id}
Authorization: Bearer <access_token>
```

Cancellation MUST:

1. mark the DAL run as cancelled
2. remove or deactivate any associated user-facing instance state
3. delete or deactivate the corresponding `HelxInst`
4. allow helxapp-controller and Kubernetes garbage collection to remove
   derived resources, except retained volumes

## 10. Run Lifecycle

The reconciled lifecycle is:

```text
registered -> launching -> running -> completed
registered -> launching -> running -> failed
registered -> cancelled
running -> cancelled
```

The combined contract SHOULD preserve the discovery lifecycle while allowing a
distinct `launching` state to represent asynchronous controller, AppStore, or
execution-manager reconciliation.

Suggested interpretation:

- `registered`: DAL run exists but launch not started
- `launching`: HeLx objects, mounts, or user-facing state are being created
- `running`: workload exists and execution has started
- `completed`: workload ended successfully
- `failed`: workload ended unsuccessfully
- `cancelled`: run terminated by request or policy

For interactive apps, readiness is separate from run status. A run may be
`running` while `is_ready` is still `false`.

## 11. Container and Runtime Contract

The combined execution contract MUST preserve the current minimum container
contract for model execution:

### 11.1 Required environment

The launched workload MUST receive:

- `MODEL_ID`
- `RUN_ID`
- `INPUT_PATH`
- `OUTPUT_PATH`

For web applications, it SHOULD also receive:

- `REMOTE_USER`
- `NB_PREFIX`
- `IDENTITY_TOKEN`, when instance callback authentication is enabled

### 11.2 Required mounts

The launched workload MUST be able to access:

- model inputs under `/input`
- model outputs under `/output`

For multiple inputs, the platform SHOULD preserve the existing numbered mount
convention.

### 11.3 Environment precedence

Where both HeLx CRDs and run-specific settings apply, the precedence SHOULD be:

```text
HelxApp service environment < HelxUser environment < HelxInst environment
```

Run-derived values SHOULD land in `HelxInst` so they take precedence over
template defaults.

## 12. Underlying Object Relationships

A single launched run has the following object graph:

```text
OIDC token (`iss`, `sub`)
    -> derived internal_username
    -> derived k8s_username
    -> HelxUser
    -> AppStore user/session context, if needed

DAL Resource(model) -> persistent HelxApp
DAL Run            -> HelxInst
DAL Resource(data) -> run-specific mounts

Execution manager
    <-> sid
    <-> DAL run correlation
    <-> HelxInst metadata/status

HelxUser + HelxApp + HelxInst
    -> Deployment
    -> Service
    -> PersistentVolumeClaim(s)
    -> Ambassador route
    -> launch URL

AppStore
    <-> user-facing access path
    <-> readiness view
    <-> optional identity token
```

The implementation SHOULD record enough correlation data to trace from a
`run_id` to:

- `sid`
- `HelxInst.metadata.name`
- `HelxInst.status.uuid`, if available
- Deployment name
- Service name
- routed URL

## 13. Readiness and URL Semantics

### 13.1 Readiness

For interactive or web-accessible executions, readiness SHOULD be based on the
underlying AppStore or controller readiness signal, such as `is_ready` or
Deployment `readyReplicas >= 1`.

For batch-only executions, `is_ready` MAY be `null` or MAY represent that the
worker pod has started.

### 13.2 URL

The `url` field MUST be returned when the workload is externally reachable.
That URL SHOULD be derived from the Ambassador route and SHOULD incorporate the
Kubernetes-safe username.

A recommended route shape is:

```text
/private/<app-class-or-app-name>/<k8s-username>/<instance-id-or-uuid>/
```

A shorter route without instance ID is acceptable only if the deployment model
guarantees one active instance per app per user.

## 14. Error Handling

The combined contract MUST preserve at least the existing classes of errors:

- `401 Unauthorized`: missing, invalid, or expired bearer token
- `403 Forbidden`: valid token but not authorized for the run
- `400 validation_error`: invalid run, missing model, missing execution
  metadata, or invalid inputs
- `404 Not Found`: run not found or not visible to caller
- `409 Conflict`: conflicting active instance or invalid lifecycle transition
- `502 orchestration_error`: execution manager, AppStore, controller, or
  Kubernetes launch failed

Error responses SHOULD identify which layer failed:

- authentication
- DAL validation
- execution-manager orchestration
- AppStore orchestration or visibility
- helxapp-controller reconciliation
- Kubernetes runtime

## 15. Audit and Observability

For every launch, the platform SHOULD record:

- `run_id`
- authenticated `iss`
- authenticated `sub`
- `internal_username`
- `k8s_username`
- `HelxUser.metadata.name`
- `HelxApp.metadata.name`
- `HelxInst.metadata.name`
- `sid`
- routed `url`, if any
- timestamps for launch, readiness, completion, and cancellation

The platform SHOULD expose a health endpoint for the combined execution API and
SHOULD log correlation IDs across DAL, execution manager, AppStore, controller,
and Kubernetes events.

## 16. Testing Requirements

Testing MUST cover the integrated contract at multiple levels.

### 16.1 API contract tests

Tests SHOULD verify:

- bearer token validation
- username derivation from (`iss`, `sub`)
- Kubernetes-safe normalization
- launch response shape compatibility with the existing discovery contract
- status, list, and cancel semantics

### 16.2 Mapping tests

Tests SHOULD verify:

- DAL model -> persistent HelxApp mapping
- authenticated user -> HelxUser mapping
- DAL run -> HelxInst mapping
- dataset locations -> mounted input paths
- `Run.environment` and parameters -> instance resources and env
- `sid` generation and persistence rules
- run-specific mount generation through `HelxInst`

### 16.3 Controller and Kubernetes tests

Tests SHOULD verify:

- the complete triple creates Deployment, Service, and PVC objects
- labels and ownership allow traceability from run to workload
- Ambassador route generation uses the Kubernetes-safe username
- deletion of `HelxInst` tears down derived resources except retained volumes
- security context resolution works through `HelxInst` and `HelxUser`
- any template extension for run-specific mounts renders correctly

### 16.4 End-to-end tests

End-to-end tests SHOULD exercise:

1. authenticate with OIDC
2. create or reference DAL model and dataset resources
3. prepare a run
4. `POST /api/v1/runs`
5. poll `GET /api/v1/runs/{run_id}`
6. verify readiness and returned URL when interactive
7. access the returned URL for web-accessible workloads
8. `DELETE /api/v1/runs/{run_id}`
9. verify cleanup of `HelxInst` and derived resources

### 16.5 Regression tests

Regression tests SHOULD preserve feature coverage from the existing discovery
execution contract, including:

- multiple input resources
- resource requirements defaults
- cancellation behavior
- final status synchronization
- URL presence for interactive workloads and `null` for batch workloads

## 17. Current Assumptions and Expectations

The current working assumptions and expectations are:

1. A parallel execution-manager deployment will operate alongside AppStore.
2. The execution manager and AppStore will manipulate or observe the same HeLx
   objects.
3. The execution manager is the authoritative source of `sid`.
4. `sid` is currently expected to be stored as an annotation on `HelxInst`.
5. If AppStore needs `sid`, it will consume or surface the value already
   associated with `HelxInst`.
6. `HelxApp` is expected to persist as a long-term configuration collection
   point.
7. Run-specific mount definitions are expected to be attached at the
   `HelxInst` layer.
8. Extending the volume or deployment template is a likely mechanism for mount
   realization.
9. helxapp-controller is expected to be extended so that selected
   `HelxInst` annotations can be propagated onto derived objects such as
   Deployments.
10. AppStore is expected to understand user-facing application components but
    not to model pipeline or batch collection semantics as a first-class
    concept.

These assumptions SHOULD remain visible during design and implementation and
SHOULD be revisited as requirements evolve.

## 18. To Be Determined

The following items remain to be determined and SHOULD be tracked as active
design questions:

1. The precise annotation key to use for `sid` on `HelxInst`
2. Which `HelxInst` annotations helxapp-controller should propagate to
   derived objects, and by what policy
3. Whether run-specific mounts belong in `HelxInst.spec`, generated template
   data, or an extension to the HeLx CRD schema
4. Whether AppStore should become fully read-only with respect to `sid`, or
   whether it may cache and re-surface it
5. The exact correlation model between DAL `Run`, `HelxInst.status.uuid`,
   `HelxInst` annotations, and any AppStore instance identifier
6. Whether batch-only runs should always remain visible through AppStore, or
   only through the execution API
7. The exact route structure for multi-instance interactive workloads
8. The precise extension points required in helxapp-controller templates,
   reconciliation, and tests to support run-specific mounts cleanly

## 19. Minimum Normative Summary

A conforming implementation of this combined contract MUST:

- accept OIDC bearer access tokens on execution endpoints
- derive a canonical identity from (`iss`, `sub`)
- derive a Kubernetes-safe lowercase username for HeLx and routing
- preserve the existing `/api/v1/runs` launch, status, list, and cancel API
- support all minimum features of the existing discovery execution contract
- map authenticated users to `HelxUser`
- map model resources to persistent `HelxApp`
- map runs to `HelxInst`
- preserve `/input` and `/output` container semantics
- create and maintain an authoritative `sid`
- store `sid` as an annotation on `HelxInst`
- support propagation of selected `HelxInst` annotations onto derived
  Kubernetes objects through helxapp-controller extensions
- return `sid`, status, readiness, and URL when applicable
- support integrated testing across API, execution manager, AppStore,
  controller, and Kubernetes
