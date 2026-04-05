Create a Mermaid flowchart that represents a unified Kubernetes execution model combining two paths:
1) an imperative AppStore/Tycho launch path
2) a declarative HelxApp Controller reconciliation path

The diagram should show that both paths converge on the same normalized execution model.

Requirements:
- Use a top-down flowchart: flowchart TD
- Make it clear that there are three control-plane inputs:
  1. Application Template
  2. User Context
  3. Instance Intent
- Show that Instance Intent can originate from either:
  - REST POST /instances (AppStore/Tycho)
  - HelxInst create/update reconcile trigger (HelxApp Controller)
- Show that Application Template can originate from either:
  - app registry + compose/.env spec
  - HelxApp CR
- Show that User Context can originate from either:
  - authenticated principal + DB token/OAuth context
  - HelxUser CR + optional remote security lookup

Main stages to include:
1. Request / Desired-State Intake
2. Registry and Identity Resolution
3. Execution-Spec Assembly
4. Artifact Generation
5. Kubernetes Apply / Reconcile
6. Runtime Exposure and Lifecycle Management

Inside “Execution-Spec Assembly”, include the normalized execution spec fields:
- workload identity
- containers
- resources
- environment variables
- security context
- volumes / storage
- ports / services
- labels / ownership metadata

Inside “Artifact Generation”, include:
- Deployment
- Service(s)
- PersistentVolumeClaim(s)
- optional NetworkPolicy / ingress / edge-route annotations

Inside “Runtime Exposure and Lifecycle Management”, include:
- running pod(s)
- stable service endpoint
- optional external route / user URL
- persistent storage mounted
- update / drift reconciliation
- deletion / teardown
- retained storage where applicable

Also show the conceptual transformation:
(User Request, App Definition, User Context, Resource Overrides, Platform Settings)
→ Execution Specification
→ Kubernetes Artifacts
→ Running Workload + Network Endpoint + Persistent Storage

Design guidance:
- Group AppStore/Tycho-specific inputs in one subgraph
- Group HelxApp Controller-specific inputs in another subgraph
- Have both feed into a shared “Unified Execution Model” pipeline
- Use one central node called “Normalized Execution Specification”
- Use a final node called “Managed Running Instance”
- Add a note or node indicating the key architectural difference:
  - AppStore/Tycho = imperative request-driven launcher
  - HelxApp Controller = declarative controller-driven reconciler
- Keep the diagram readable and architecture-focused, not code-focused
- Prefer subgraphs for:
  - Source Systems
  - Unified Pipeline
  - Kubernetes Runtime
- Use concise node labels but preserve the concepts above

The output should be only Mermaid code, no explanation text.
