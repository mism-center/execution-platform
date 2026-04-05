# HelxApp Controller — Execution Model

## Overview

The helxapp-controller is a Kubernetes operator built with controller-runtime. It watches three Custom Resource Definition (CRD) kinds — **HelxApp**, **HelxInst**, and **HelxUser** — and, when all three exist and are correlated, synthesizes the Kubernetes workload objects (Deployment, Services, PersistentVolumeClaims) that actually run the application pod.

---

## Custom Resource Kinds

### HelxApp — the application template

A `HelxApp` describes *what* an application is: its container images, ports, environment variables, volumes, resource bounds, and security context overrides. It is cluster-wide in scope (admin-created) but can be namespaced.

Key fields (`HelxAppSpec`):

| Field | Purpose |
|-------|---------|
| `appClassName` | Logical class name (e.g. `JupyterLab`), stamped onto pod labels and passed to templates |
| `sourceText` | Optional raw source text (not used in current template path) |
| `services[]` | Ordered list of `Service` records, one per container |

Each `Service` entry carries:

| Field | Purpose |
|-------|---------|
| `name` | Container name; also used as the resource-lookup key in `HelxInst.resources` |
| `image` | Docker image reference, optionally followed by `,key=value` options (e.g. `,Always`) |
| `command[]` | Optional override for the container entrypoint; may contain Go template expressions |
| `environment` | Map of `NAME: value` env vars; may contain Go template expressions |
| `init` | If `true`, this service becomes an init container |
| `ports[]` | `containerPort` / `port` pairs; a non-zero `port` means a Kubernetes Service is needed |
| `resourceBounds` | Per-resource `min`/`max` bounds (advisory; the instance overrides actual requests/limits) |
| `securityContext` | Per-container UID/GID/FSGroup/supplementalGroups |
| `volumes` | Map of `volumeId → volume-source string` (see Volume DSL below) |

### HelxInst — the instance request

A `HelxInst` is a per-user instantiation request: "run this app for this user". It is the *trigger* for workload creation.

Key fields (`HelxInstSpec`):

| Field | Purpose |
|-------|---------|
| `appName` | Name (or `namespace/name`) of the `HelxApp` to instantiate |
| `userName` | Name (or `namespace/name`) of the `HelxUser` who owns this instance |
| `securityContext` | Instance-level security context; overrides user-fetched context when present |
| `resources` | Map of `serviceName → {requests, limits}` — per-container resource requests/limits |

Status:

| Field | Purpose |
|-------|---------|
| `observedGeneration` | Prevents redundant reconciliation |
| `uuid` | A UUID assigned on first reconciliation; used to label and identify all derived objects |

### HelxUser — the user record

A `HelxUser` represents a platform user. Its `userHandle` is a URL that the controller can call to obtain security context information (uid/gid/fsGroup) for the pod.

| Field | Purpose |
|-------|---------|
| `userHandle` | Optional URL; the controller performs an HTTP GET and parses the JSON response for `runAsUser`, `runAsGroup`, `fsGroup`, `supplementalGroups` |

---

## In-Memory Object Graph

Because the three CRDs arrive independently and in any order, the controller maintains an in-memory relational graph (`helxapp_operations` package):

```
appTable   map[string]TableElement[HelxApp]
             └─ Obj     *HelxApp
             └─ InstSet map[string]bool   ← set of instance names

userTable  map[string]TableElement[HelxUser]
             └─ Obj     *HelxUser
             └─ InstSet map[string]bool

instanceTable map[string]InstTableElement
             └─ Inst    HelxInst
```

The maps are keyed by `namespace/name`. Every time any of the three CRDs is reconciled, `AddApp`, `AddUser`, or `AddInst` updates the graph and maintains the bidirectional associations. If an app or user arrives after instances already exist (or vice versa), the returning `instList` from `addObjToMap` triggers deferred workload creation for the newly-complete triples.

---

## Reconciliation Flow

```
User creates/updates HelxInst
         │
         ▼
HelxInstReconciler.Reconcile()
  ├─ Fetch HelxInst from API server
  ├─ If deleted → DeleteInst() → return
  ├─ If ObservedGeneration >= Generation → AddInst() (resync graph only) → return
  ├─ Assign UUID if new
  ├─ AddInst() → updates graph, no returned insts
  ├─ CreateDerivatives(helxInst, ...)
  └─ defer: update Status.ObservedGeneration
```

```
User creates/updates HelxApp
         │
         ▼
HelxAppReconciler.Reconcile()
  ├─ Fetch HelxApp
  ├─ If deleted → DeleteApp() → DeleteDerivatives() for each associated inst
  ├─ If resync → AddApp() → return
  ├─ AddApp() → returns list of instances already linked to this app
  └─ For each inst in list → CreateDerivatives()
```

```
User creates/updates HelxUser  (same pattern as HelxApp)
         │
         ▼
HelxUserReconciler.Reconcile()
  ├─ AddUser() → returns associated instances
  └─ For each inst → CreateDerivatives()
```

**Invariant**: `CreateDerivatives` only produces output when both the `HelxApp` and `HelxUser` referenced by the instance are present in the graph.

---

## Artifact Generation Pipeline

`CreateDerivatives → GenerateArtifacts → renderObject`

### Step 1 — Resolve the app and user

```go
app  := GetApp(appName)    // lookup in appTable
user := GetUser(userName)  // lookup in userTable
```

Both must be non-nil; otherwise `GenerateArtifacts` returns `(nil, nil)`.

### Step 2 — Transform CRD data into template types

`transformApp(instance, app)` iterates over `app.Spec.Services` and builds a slice of `template_io.Container` values:

- **Ports**: each `PortMap` is copied; `hasService = true` when `port != 0`.
- **Volumes**: each `volumeId → volumeStr` entry is parsed by the Volume DSL (see below) into a `Volume` (pod-level source) and a `VolumeMount` (container-level path).
- **Resources**: `instance.Spec.Resources[serviceName]` provides actual `Requests` and `Limits`.
- **Image**: split at first comma — the image reference, then `key[=value]` option flags (e.g. `Always` sets `imagePullPolicy: Always`).
- **Security context**: copied from the `service.SecurityContext` field.

### Step 3 — Build the System context

```go
system := template_io.System{
    AppClassName: ...,
    AppName:      ...,
    InstanceName: ...,
    UUID:         instance.Status.UUID,
    UserName:     ...,
    Containers:   containers,    // regular containers
    Volumes:      volumes,       // all unique volume sources
    Environment:  systemEnv,     // GUID, USER, HOST, APP_CLASS_NAME, APP_NAME, INSTANCE_NAME
    SecurityContext: ...,        // resolved below
}
```

**Security context resolution** (priority order):
1. `instance.Spec.SecurityContext` — explicit per-instance override
2. `user.Spec.UserHandle` URL — HTTP GET → JSON with `runAsUser`, `runAsGroup`, `fsGroup`, `supplementalGroups`
3. No security context (omitted from pod spec)

### Step 4 — Template rendering

Three template families produce YAML strings via `renderObject`:

| Template | Input | Output |
|----------|-------|--------|
| `deployment` | `system` | One `apps/v1 Deployment` |
| `pvc` | `system` + one `Volume` | One `v1 PersistentVolumeClaim` per `pvc://` volume |
| `service` | `system` + one `Container` | One `v1 Service` per container with `hasService=true` |

Rendering is **double-pass**: after the Go template engine renders the template, `ReRender` re-renders the resulting YAML as a Go template itself. This allows field values inside `HelxApp` (e.g. volume names, commands) to reference `{{ .system.UserName }}` and have that resolved at instantiation time.

### Step 5 — Apply to the cluster

For each artifact:

- `DeploymentFromYAML` / `PVCFromYAML` / `ServiceFromYAML` decode the YAML string into a typed Kubernetes object.
- `CreateOrUpdateResource` checks whether the object already exists:
  - **Not found** → Create. If the `helx.renci.org/retain: "true"` label is absent, a controller owner reference is set so the object is garbage-collected when the `HelxInst` is deleted.
  - **Found** → Compute a JSON Patch (diff between existing and desired), filter operations (PVCs block `remove` operations to protect bound claims), then apply via `client.Patch`.

---

## Volume DSL

Volume sources in `HelxApp.Spec.Services[*].Volumes` use a mini-DSL:

```
[scheme://]src:mountPath[#subPath][,option[=value]...]
```

| Segment | Description |
|---------|-------------|
| `scheme` | `pvc` (default) or `nfs` |
| `src` | PVC claim name, or NFS server path (`//server/path`) |
| `mountPath` | Container mount point |
| `subPath` | Optional sub-directory within the volume |
| `retain` | Option flag; adds `helx.renci.org/retain: true` label to the PVC, preventing deletion |
| `rwx` / `rox` / `rwop` | PVC access mode flags (default `ReadWriteOnce`) |
| `size` | PVC storage size (default `1G`) |
| `storageClass` | PVC storage class name |

The `volumeId` map key becomes the Kubernetes volume name within the pod spec.

---

## Kubernetes Objects Produced

For a `HelxInst` whose associated `HelxApp` defines N services:

```
Deployment  (always one)
  └─ Pod template
       ├─ initContainers  (services with init:true)
       ├─ containers      (all other services)
       └─ volumes         (union of all volume sources)

PersistentVolumeClaim  (one per unique pvc:// volume across all services)

Service  (one per service that declares at least one port with a non-zero port)
```

All derived objects share the label `helx.renci.org/id: <UUID>`, which ties them to the owning `HelxInst` and is used for set-based deletion.

---

## Deletion

| Trigger | Effect |
|---------|--------|
| `HelxInst` deleted | `DeleteInst()` removes from graph; Kubernetes owner-reference GC removes Deployment, Services, PVCs (unless `retain=true`) |
| `HelxApp` deleted | `DeleteApp()` → `DeleteDerivatives()` — explicit label-selector delete for Deployment, PVCs, Services for every associated inst |
| `HelxUser` deleted | Same as HelxApp deletion for all instances linked to that user |

Objects with `helx.renci.org/retain: "true"` are excluded from explicit deletion, allowing persistent volumes to survive instance teardown.

---

## Label Taxonomy

| Label | Value | Stamped on |
|-------|-------|-----------|
| `executor` | `helxapp-controller` | All derived objects |
| `helx.renci.org/id` | Instance UUID | All derived objects (selector for deletion) |
| `helx.renci.org/app-name` | App name | Deployment, pod template |
| `helx.renci.org/username` | User name | Deployment, pod template |
| `helx.renci.org/app-class-name` | App class | Pod template |
| `helx.renci.org/instance-name` | Instance name | Pod template |
| `helx.renci.org/retain` | `"true"` | PVCs that should survive deletion |

---

## Sequence Diagram (Happy Path)

```
kubectl apply HelxApp     kubectl apply HelxUser    kubectl apply HelxInst
       │                          │                          │
       ▼                          ▼                          ▼
 HelxAppReconciler           HelxUserReconciler        HelxInstReconciler
 AddApp() → []               AddUser() → []            AddInst()
 (no insts yet)              (no insts yet)            assign UUID
                                                       CreateDerivatives()
                                                         GenerateArtifacts()
                                                           transformApp()
                                                           build System{}
                                                           render "deployment"
                                                           render "pvc" × N
                                                           render "service" × M
                                                         DeploymentFromYAML → Create
                                                         PVCFromYAML × N   → Create
                                                         ServiceFromYAML × M → Create
                                                               │
                                                               ▼
                                                        Kubernetes schedules Pod
```

If `HelxInst` arrives before `HelxApp` or `HelxUser`, `GenerateArtifacts` returns nil and no workload is created. When the missing resource later arrives, its reconciler calls `AddApp`/`AddUser`, receives the waiting instance in the returned `instList`, and calls `CreateDerivatives` to complete the workload.
