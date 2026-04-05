# Unified Execution Model

A single execution model that combines the **AppStore/Tycho launch path** and the **HelxApp Controller reconciliation path** into one cohesive description of how a user request becomes a running Kubernetes workload.

---

## Executive Summary

Both systems implement the same core lifecycle:

1. **A user or API submits an instance request** for a named application.
2. **Application definition and user identity are resolved** from control-plane state.
3. **Requested resources, security context, environment, ports, and volumes are merged** into a normalized execution description.
4. **Kubernetes objects are rendered or synthesized** from that description.
5. **The cluster applies those objects** and schedules the workload.
6. **Networking and storage are attached** so the user receives a stable URL and persistent state where applicable.
7. **Lifecycle ownership is maintained** so updates and deletions reconcile derived objects.

The main difference is architectural style:

- **AppStore/Tycho** is an **imperative request-driven launcher**. A REST API validates a launch request and immediately calls into a runtime that creates Kubernetes resources.
- **HelxApp Controller** is a **declarative controller-driven launcher**. Custom resources describe app, user, and instance state; the operator continuously reconciles those into Kubernetes resources.

Despite this difference, both systems can be described through one common execution contract.

---

## The Canonical Execution Contract

Every launched workload can be modeled as the output of this transformation:

```text
(User Request, App Definition, User Context, Resource Overrides, Platform Settings)
    → Execution Specification
    → Kubernetes Artifacts
    → Running Workload + Network Endpoint + Persistent Storage
```

### Execution Specification

The normalized execution specification contains:

- **Workload identity**
  - application name/class
  - instance name / system name
  - unique execution ID / UUID
  - owning user
- **Containers**
  - image
  - command / entrypoint
  - environment variables
  - exposed ports
  - liveness / readiness behavior
- **Resources**
  - CPU, memory, GPU, ephemeral storage
  - requests and limits per container
- **Security context**
  - runAsUser
  - runAsGroup
  - fsGroup
  - supplemental groups when supported
- **Storage**
  - shared/user home volumes
  - PVC-backed volumes
  - optional retained volumes
  - mount paths and subpaths
- **Networking**
  - in-cluster service exposure
  - ingress or edge routing annotations when used
- **Ownership metadata**
  - labels that tie all derived objects to the execution instance
  - owner references or label selectors for cleanup

This is the conceptual center of both implementations.

---

## Unified Layer Model

The two source systems can be collapsed into six common layers.

```text
Launch Intent
    │
    ▼
1. Request / Desired-State Intake
    │
    ▼
2. Registry and Identity Resolution
    │
    ▼
3. Execution-Spec Assembly
    │
    ▼
4. Artifact Generation
    │
    ▼
5. Kubernetes Apply / Reconcile
    │
    ▼
6. Runtime Exposure and Lifecycle Management
```

### 1) Request / Desired-State Intake

This is where execution begins.

**AppStore/Tycho**
- A client sends `POST /api/v1/instances/`.
- The API authenticates the caller, validates request fields, checks resource bounds, creates an identity token, and invokes the launcher.

**HelxApp Controller**
- A `HelxInst` object is created or updated in Kubernetes.
- Reconciliation observes the desired instance and treats it as the trigger for workload creation.

**Unified interpretation**
- The platform receives a request to instantiate **application X for user Y** with optional resource and security overrides.

### 2) Registry and Identity Resolution

The system must resolve what to run and who it runs for.

**AppStore/Tycho**
- `app_id` resolves through the Tycho app registry.
- The registry points to a `docker-compose`-style app spec and `.env` defaults.
- User identity is packaged as a `Principal` plus a persisted `UserIdentityToken`.

**HelxApp Controller**
- `HelxInst.spec.appName` resolves to a `HelxApp` CR.
- `HelxInst.spec.userName` resolves to a `HelxUser` CR.
- Security context may be fetched from `HelxUser.spec.userHandle` via HTTP.

**Unified interpretation**
- The platform resolves two authoritative inputs:
  1. **Application template**: container topology, ports, env, probes, volumes, defaults.
  2. **User context**: username, tokenized identity, security context, and ownership information.

### 3) Execution-Spec Assembly

This layer normalizes disparate inputs into a runtime-ready model.

**AppStore/Tycho**
- Resource overrides are merged over the app spec.
- Identity env vars are injected.
- Security defaults are applied.
- `System.parse()` converts the merged structure into typed `System` and `Container` objects.

**HelxApp Controller**
- `transformApp()` converts `HelxApp.spec.services[]` into container definitions.
- `HelxInst.spec.resources` supplies actual per-container requests and limits.
- Volume strings are parsed by the volume DSL.
- Security context resolves in priority order: instance override, then user lookup, then omitted.
- A `template_io.System` is constructed.

**Unified interpretation**
- This layer produces a **normalized execution specification** that is independent of how the app was described upstream.
- The key outcome is a structure that contains all container, storage, security, and network information necessary to emit Kubernetes objects.

### 4) Artifact Generation

The normalized execution spec is translated into Kubernetes manifests or typed resources.

**AppStore/Tycho**
- Jinja2 templates render pod and service YAML.
- A pod template may include init containers, shared PVC mounts, env vars, and probes.
- The pod template is later wrapped into a `Deployment`.

**HelxApp Controller**
- Go templates generate:
  - one `Deployment`
  - one `PersistentVolumeClaim` per PVC volume
  - one `Service` per externally exposed container
- Rendering is double-pass so fields in CRD content can themselves contain template expressions.

**Unified interpretation**
- Artifact generation is the step where the abstract execution model becomes a concrete set of Kubernetes objects.
- The exact templating technology differs, but the conceptual output is the same.

### 5) Kubernetes Apply / Reconcile

The desired artifacts are pushed into cluster state.

**AppStore/Tycho**
- The Kubernetes client loads configuration.
- It creates a `Deployment`, `Service`, and optional `NetworkPolicy`.
- The API call path is direct and imperative.

**HelxApp Controller**
- Decoded artifacts are applied with create-or-update semantics.
- Existing resources are patched rather than blindly recreated.
- PVC patch behavior is restricted to avoid destructive updates.
- Owner references or labels determine garbage collection and explicit deletion.

**Unified interpretation**
- This layer guarantees that the cluster converges on the desired execution state.
- In imperative form, convergence happens immediately after the request.
- In controller form, convergence happens continuously through reconciliation.

### 6) Runtime Exposure and Lifecycle Management

Once the workload is running, the platform maintains access and cleanup behavior.

**AppStore/Tycho**
- Services expose pod ports.
- Ambassador annotations create per-user routes.
- Shared NFS-backed home storage can be mounted.
- Identity tokens allow the running app to authenticate callbacks.

**HelxApp Controller**
- Services are created for containers that declare routable ports.
- PVCs provide persistent storage.
- Label sets tie objects back to the `HelxInst` UUID.
- Deletion and retention rules govern cleanup.

**Unified interpretation**
- A launch is complete only when three things exist:
  1. **A running workload**
  2. **A reachable endpoint**
  3. **A managed lifecycle** for update and teardown

---

## End-to-End Unified Flow

```text
User / API Client / Control Plane
        │
        │  launch application for user with resource overrides
        ▼
Request Intake
  ├─ AppStore: REST POST /instances
  └─ HelxApp: create/update HelxInst
        │
        ▼
Resolve Control-Plane Inputs
  ├─ application template
  ├─ user context
  ├─ platform defaults
  └─ existing instance metadata / UUID
        │
        ▼
Assemble Normalized Execution Specification
  ├─ merge env
  ├─ merge resources
  ├─ resolve security context
  ├─ resolve ports and services
  ├─ resolve volumes and mounts
  └─ stamp identity labels and IDs
        │
        ▼
Generate Kubernetes Artifacts
  ├─ Deployment
  ├─ Service(s)
  ├─ PVC(s)
  └─ optional NetworkPolicy / ingress annotations
        │
        ▼
Apply / Reconcile with Kubernetes
  ├─ create or patch resources
  ├─ attach owner metadata
  └─ rely on scheduler to place pods
        │
        ▼
Runtime State
  ├─ running pod(s)
  ├─ stable in-cluster service(s)
  ├─ optional edge route / user URL
  └─ persistent storage where configured
        │
        ▼
Lifecycle Management
  ├─ update on spec change
  ├─ reconcile drift
  ├─ preserve retained storage
  └─ delete derived resources on teardown
```

---

## Mapping the Two Implementations to One Model

| Unified concept | AppStore/Tycho | HelxApp Controller |
|---|---|---|
| Launch trigger | `POST /api/v1/instances/` | `HelxInst` reconcile |
| App definition source | app registry + compose spec + `.env` | `HelxApp` CR |
| User definition source | authenticated request + DB token + OAuth context | `HelxUser` CR + optional HTTP user-handle lookup |
| Instance record | API-layer instance request and returned `sid` | `HelxInst` CR + status UUID |
| Normalized execution model | `System` / `Container` objects | `template_io.System` / container set |
| Manifest generation | Jinja2 templates | Go templates |
| K8s apply mode | direct client create calls | controller create-or-update patch loop |
| Routing | Service + Ambassador annotation | Service objects; ingress externalization depends on surrounding platform |
| Persistent storage | shared home PVC + template mounts | PVC DSL + retain / access-mode controls |
| Cleanup model | direct launch path plus K8s resources tied by labels | owner refs + explicit delete by label selector |

---

## Canonical Resource Semantics

### Application Definition

An application definition describes the *shape* of an executable workload:

- one or more containers
- commands and environment
- internal and external ports
- health behavior
- volume declarations
- security defaults
- optional image pull behavior

In AppStore/Tycho, this shape comes from a compose-oriented app registry.
In HelxApp Controller, it comes from CRD-native service definitions.

### Instance Request

An instance request binds the application definition to a specific user and runtime configuration:

- user identity
- per-container resources
- optional security overrides
- launch-time environment additions
- instance UUID / execution identifier

### Execution Identifier

Both systems stamp a unique identifier on derived resources:

- **AppStore/Tycho** uses `tycho-guid`
- **HelxApp Controller** uses `helx.renci.org/id`

The identifier serves the same role in both systems:

- correlate all derived resources
- enable selection and cleanup
- associate runtime state back to the originating launch record

---

## Security and Identity Model

A unified security model emerges across both systems.

### Identity

The workload runs on behalf of an authenticated platform user.
That identity is represented through a combination of:

- username or user name
- execution-scoped ID / UUID / SID
- environment variables injected into the container
- optional persisted callback token

### Pod Security Context

Both implementations support pod or container security context materialization:

- `runAsUser`
- `runAsGroup`
- `fsGroup`
- supplemental groups where supported

Resolution precedence can be described generically as:

1. explicit instance override
2. app or user-derived defaults
3. platform default or omission

### Callback / Control-Plane Trust

AppStore/Tycho explicitly injects an `IDENTITY_TOKEN` that allows the running workload to authenticate back to the control plane. In the unified model, this is one example of a broader pattern:

- the platform may inject execution-scoped credentials or tokens so the running workload can participate in managed workflows securely.

---

## Storage Model

Both systems support persistent storage, but express it differently.

### Shared conceptual model

Storage is attached through a volume abstraction with these common properties:

- source type
- source identifier
- mount path
- optional subpath
- retention behavior
- access mode and capacity where applicable

### AppStore/Tycho expression

- shared NFS-backed user home PVC
- optional init container to create user directories
- consistent mount path based on username

### HelxApp Controller expression

- explicit volume DSL
- per-volume PVC creation
- retain labels
- access mode and storage-class selection

### Unified interpretation

Storage belongs in the execution spec as a first-class component, not as an afterthought. It is part of the application contract and affects both startup and teardown behavior.

---

## Networking Model

Both systems expose workloads through Kubernetes Services and optionally through higher-level routing.

### Shared conceptual model

- containers declare ports
- routable ports produce Service objects
- edge routing may be attached through annotations or external controllers
- the user ultimately receives an addressable endpoint for the instance

### AppStore/Tycho expression

- a `Service` is created
- Ambassador annotations can publish a per-instance path-based route

### HelxApp Controller expression

- a `Service` is generated for containers with a non-zero exposed service port
- external ingress is left to adjacent platform components

### Unified interpretation

The execution system owns in-cluster connectivity; edge publication may be native or delegated.

---

## Reconciliation and Lifecycle Model

A single lifecycle story can also describe both systems.

### Create

- resolve app + user + instance intent
- synthesize execution spec
- create derived Kubernetes resources
- wait for scheduler convergence

### Update

- resource changes, template changes, or identity/security changes produce a new desired state
- the platform regenerates artifacts and reapplies or patches them

### Delete

- deleting the originating instance removes or garbage-collects derived resources
- retention rules may preserve storage beyond the execution lifetime

### Drift handling

- AppStore/Tycho is primarily launch-time imperative, so drift handling depends more on Kubernetes primitives after creation.
- HelxApp Controller is inherently drift-correcting because reconciliation continuously compares desired and actual state.

### Unified interpretation

The more general model is:

> An execution is a set of Kubernetes resources derived from higher-level control-plane intent, continuously or immediately converged into cluster state, and deleted according to ownership and retention policy.

---

## Where the Two Systems Differ Most

### 1. Imperative vs declarative control plane

- **AppStore/Tycho**: launch happens synchronously from an API request.
- **HelxApp Controller**: launch happens asynchronously through CRD reconciliation.

### 2. App description format

- **AppStore/Tycho**: external registry + compose-like input
- **HelxApp Controller**: CRD-native application schema

### 3. User representation

- **AppStore/Tycho**: request-authenticated principal and DB-backed token
- **HelxApp Controller**: `HelxUser` resource plus optional remote security lookup

### 4. Apply mechanics

- **AppStore/Tycho**: direct create calls via the Kubernetes Python client
- **HelxApp Controller**: create-or-update patch semantics through controller-runtime

### 5. Cleanup sophistication

- **AppStore/Tycho**: resource grouping primarily via labels and launch-time logic
- **HelxApp Controller**: richer ownership, retention, and explicit delete flows

These are implementation differences, not conceptual contradictions.

---

## Recommended Canonical Framing

If this combined model is used as the reference execution description for the platform, the cleanest framing is:

### Control-plane objects

- **Application Template** — defines what can run
- **User Context** — defines who it runs for and under what security identity
- **Instance Intent** — defines that a specific user wants a specific app now, with concrete runtime overrides

### Runtime synthesis

These three inputs are merged into a **Normalized Execution Specification**.

### Kubernetes realization

That specification is realized as:

- one **Deployment** for workload execution
- zero or more **Services** for network exposure
- zero or more **PersistentVolumeClaims** for durable state
- optional policy or ingress objects as required by the platform

### Runtime contract

The launched instance is identified by a unique execution ID and remains manageable through labels, owner references, and reconcile/update semantics.

---

## Canonical Unified Diagram

```text
                    ┌─────────────────────────────┐
                    │      Application Template   │
                    │  (registry entry / HelxApp) │
                    └──────────────┬──────────────┘
                                   │
                                   │
┌─────────────────────┐            ▼            ┌─────────────────────┐
│     User Context    │────────────────────────▶│   Instance Intent   │
│ (principal/HelxUser)│                         │ (API request/HelxInst)
└────────────┬────────┘                         └────────────┬────────┘
             │                                               │
             └──────────────────────┬────────────────────────┘
                                    ▼
                     ┌─────────────────────────────┐
                     │ Normalized Execution Spec   │
                     │                             │
                     │ • identity                  │
                     │ • containers                │
                     │ • resources                 │
                     │ • env                       │
                     │ • security context          │
                     │ • volumes                   │
                     │ • ports / services          │
                     │ • labels / ownership        │
                     └──────────────┬──────────────┘
                                    │
                                    ▼
                     ┌─────────────────────────────┐
                     │ Kubernetes Artifacts        │
                     │ • Deployment                │
                     │ • Service(s)                │
                     │ • PVC(s)                    │
                     │ • optional policies/routes  │
                     └──────────────┬──────────────┘
                                    │
                                    ▼
                     ┌─────────────────────────────┐
                     │ Running Workload            │
                     │ • Pod(s) scheduled          │
                     │ • endpoint exposed          │
                     │ • storage mounted           │
                     │ • lifecycle reconciled      │
                     └─────────────────────────────┘
```

---

## Final Synthesis

The two source documents describe different generations of the same platform concern: **turning higher-level application intent into managed Kubernetes runtime state**.

The unified execution model is therefore:

> A platform-controlled process that resolves an application template, user context, and instance intent into a normalized execution specification, materializes that specification as Kubernetes resources, and maintains the resulting workload through routing, storage, identity, update, and deletion semantics.

In practical terms:

- **AppStore/Tycho** is the request-time execution path.
- **HelxApp Controller** is the reconciliation-time execution path.
- The common abstraction above is the right single mental model for both.
