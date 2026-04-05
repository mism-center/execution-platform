# Vivarium-Core: Execution Model Analysis & Kafka/Polyglot Reimplementation

## Table of Contents

1. [Current Execution Model](#1-current-execution-model)
2. [Kafka Mapping](#2-kafka-mapping)
3. [Language Candidates](#3-language-candidates)
4. [Module Decomposition](#4-module-decomposition)
5. [Feature Parity Matrix](#5-feature-parity-matrix)
6. [Gaps & Risks](#6-gaps--risks)
7. [Test Strategy](#7-test-strategy)
8. [Migration Path](#8-migration-path)

---

## 1. Current Execution Model

### 1.1 Core Abstractions

| Abstraction | Role | Key Properties |
|---|---|---|
| **Process** | Stateless update function | Declares `ports_schema()`, computes `next_update(timestep, states)`, owns a `time_step` |
| **Step** | Ordered intra-tick computation | Like Process but runs every tick in dependency order (DAG via `flow`) |
| **Store** | Hierarchical state tree | Tree of typed leaves; each leaf has `_updater`, `_divider`, `_serializer`, `_emit` |
| **Topology** | Wiring diagram | Maps each process's logical ports to absolute store paths |
| **Composite** | Assembly unit | Bundle of processes + steps + topology + flow + initial state |
| **Composer** | Factory | Generates a Composite from config (abstract `generate_processes` / `generate_topology`) |
| **Engine** | Orchestrator | Owns the global clock, the store, and the simulation loop |
| **Emitter** | Sink | Receives periodic snapshots (RAM, MongoDB, stdout, null) |

### 1.2 Simulation Loop (pseudo-code)

```
engine.update(duration):
    end_time = global_time + duration
    while global_time < end_time:
        for each process p:
            if front[p].time <= global_time:
                states   = topology_view(p)            # read
                timestep = p.calculate_timestep(states)
                if p.update_condition(timestep, states):
                    deferred = Defer(p.send_command('next_update', timestep, states))
                front[p].time += timestep
                front[p].update = deferred

        full_step = min(front[p].time - global_time for all p)
        global_time += full_step

        for each process p where front[p].time <= global_time:
            update = front[p].update.get()             # may block on parallel pipe
            absolute = inverse_topology(p.path, update, p.topology)
            store.apply_update(absolute)               # write via updater fns

        run_steps()   # DAG layers, sequential within layer
        emit()        # if emit interval reached
```

### 1.3 Concurrency Model

- **Inter-process**: `multiprocessing.Pipe` per `_parallel=True` process; forkserver/spawn context.
- **Intra-tick steps**: Topologically sorted into layers; each layer *could* be parallel but currently runs sequentially.
- **No async/await**: Everything is synchronous or pipe-blocked.
- **State contention**: None by design -- updates are deferred and applied atomically between ticks.

### 1.4 State Mutation Primitives

Updates are nested dicts keyed by store paths. Leaf values are applied through registered **updater** functions:

| Updater | Semantics |
|---|---|
| `accumulate` | `current + delta` (default) |
| `set` | `delta` (overwrite) |
| `merge` | deep dict merge |
| `nonnegative_accumulate` | `max(0, current + delta)` |
| `null` | ignore |

Structural mutations use reserved keys: `_add`, `_delete`, `_move`, `_divide`, `_generate`.

### 1.5 Serialization

Custom `Serializer` registry handles numpy arrays, Pint units, sets, processes, and functions. Final wire format: `orjson` with fallback encoders. Database emitter targets MongoDB/BSON.

---

## 2. Kafka Mapping

### 2.1 Conceptual Alignment

| Vivarium Concept | Kafka Analog | Notes |
|---|---|---|
| Store (state tree) | **Compacted topic** or **KTable** (Kafka Streams) | One topic per store subtree; key = store path, value = leaf state |
| Process.next_update | **Stream processor** (consumer + producer) | Reads state from input topics, writes update to output topic |
| Topology | **Topic routing / stream joins** | Declared as consumer group subscriptions + producer targets |
| Engine tick | **Punctuator** or **external coordinator** | Advances logical time, triggers process invocations |
| Updater functions | **Aggregator** in KTable or custom merge in state-store | `accumulate` = reduce with `+`; `set` = latest-value |
| Step DAG | **Kafka Streams topology** with `addProcessor` dependency edges | Or sequential sub-topics with barrier synchronization |
| Emitter | **Sink connector** (Kafka Connect) | JDBC, S3, MongoDB, stdout sinks already exist |
| `_divide` / `_add` / `_delete` | **Tombstone + new keyed records** | Structural mutations become topic key lifecycle events |
| Parallel process | **Partition-level parallelism** | Each process instance is a partition consumer; Kafka handles distribution |

### 2.2 Topic Design

```
vivarium.{sim_id}.state.{store_path}     # compacted, keyed by leaf name
vivarium.{sim_id}.updates.{process_name} # append-only, keyed by tick
vivarium.{sim_id}.ticks                  # coordination: tick number + wall clock
vivarium.{sim_id}.commands               # process lifecycle (start/stop/divide)
vivarium.{sim_id}.emissions              # sink-ready snapshots
```

**Partitioning strategy**: partition by agent/compartment ID for hierarchical simulations; single-partition for small flat models.

### 2.3 Tick Coordination

The current engine is a **centralized discrete-event scheduler**. With Kafka, two approaches:

| Approach | Description | Trade-off |
|---|---|---|
| **Coordinator service** | Single process publishes tick barriers to `ticks` topic; processors consume, compute, produce updates, ack. Coordinator waits for all acks before advancing. | Simple, preserves current semantics exactly. Bottleneck at coordinator. |
| **Barrier-free optimistic** | Each processor tracks its own logical clock. Updates carry vector timestamps. Consumers only apply updates when causally ready. | Higher throughput, eventual consistency, complex conflict resolution. |

**Recommendation**: Start with the coordinator approach -- it is a direct 1:1 mapping of the current `Engine.run_for` loop and preserves determinism.

### 2.4 State Materialization

Kafka Streams' **state stores** (RocksDB-backed KTables) replace the in-memory `Store` tree:

- Each store path becomes a key prefix in a state store.
- Updater functions become custom `Aggregator<K, V>` implementations.
- Schema metadata (`_updater`, `_divider`, `_emit`) stored in a config topic or sidecar.
- `topology_view` becomes a **join** across relevant KTable partitions.

### 2.5 What Kafka Gives You For Free

- **Horizontal scaling**: Partition processes across nodes without custom IPC.
- **Durability & replay**: Full event log; restart from any tick.
- **Monitoring**: Prometheus metrics, consumer lag = simulation backpressure signal.
- **Language agnosticism**: Any language with a Kafka client can implement a process.
- **Schema evolution**: Avro/Protobuf schema registry for state and update formats.

---

## 3. Language Candidates

### 3.1 Comparison Matrix

| Criterion | Python (current) | Rust | Go | Java/Kotlin (JVM) |
|---|---|---|---|---|
| **Kafka ecosystem** | confluent-kafka (librdkafka wrapper) | rdkafka crate | confluent-kafka-go, Sarama | Native Kafka Streams, best-in-class |
| **Numerical perf** | numpy/scipy (C under hood) | nalgebra, ndarray (native) | gonum (adequate) | ND4J, Apache Commons Math |
| **Concurrency** | GIL limits; multiprocessing | async + threads, zero-cost | goroutines, channels | Virtual threads (Loom), reactive |
| **Type safety** | Runtime only (mypy optional) | Compile-time, ownership | Compile-time, interfaces | Compile-time, generics |
| **Serialization** | orjson, pickle | serde (fastest) | encoding/json, protobuf | Jackson, Avro native |
| **Scientific ecosystem** | Dominant (numpy, scipy, pint) | Growing but sparse | Minimal | Decent (JFreeChart, Tablesaw) |
| **Ease of process authoring** | Highest (target audience is biologists) | Steep learning curve | Moderate | Moderate |
| **Interop with existing models** | Native | FFI to Python via PyO3 | cgo or subprocess | Jython or subprocess |

### 3.2 Recommended Architecture: Polyglot with Kafka

```
┌─────────────────────────────────────────────────────────┐
│                    Kafka Cluster                         │
│  topics: state, updates, ticks, commands, emissions      │
└──────────┬──────────────┬───────────────┬───────────────┘
           │              │               │
    ┌──────┴──────┐ ┌─────┴─────┐  ┌──────┴──────┐
    │  Tick       │ │  Process   │  │  Process    │
    │  Coordinator│ │  (Python)  │  │  (Rust)     │
    │  (Rust/Go)  │ │  Bio model │  │  Perf-crit  │
    └─────────────┘ └───────────┘  └─────────────┘
           │
    ┌──────┴──────┐
    │  State      │
    │  Materializer│
    │  (JVM/KStreams)│
    └──────┬──────┘
           │
    ┌──────┴──────┐
    │  Emitter    │
    │  (Kafka     │
    │   Connect)  │
    └─────────────┘
```

- **Process implementations**: Stay in Python for biologist-authored models; rewrite hot-path processes in Rust.
- **Coordinator + state materializer**: JVM (Kafka Streams native) or Rust (rdkafka + custom state store).
- **Emitters**: Kafka Connect sinks -- zero custom code for MongoDB, S3, PostgreSQL.

---

## 4. Module Decomposition

### 4.1 Module Map

```
vivarium-kafka/
├── proto/                          # Protobuf/Avro schemas
│   ├── state.proto                 #   Store leaf values, schema metadata
│   ├── update.proto                #   Process update payloads
│   ├── tick.proto                  #   Tick barriers, acks
│   └── command.proto               #   Lifecycle events (add/delete/divide/move)
│
├── core/                           # Language-agnostic contracts
│   ├── process_trait               #   Interface: ports_schema, next_update, calculate_timestep
│   ├── updater_trait               #   Interface: apply(current, delta) -> new
│   ├── divider_trait               #   Interface: divide(value) -> [v1, v2]
│   └── serializer_trait            #   Interface: serialize/deserialize
│
├── coordinator/                    # Tick scheduler (Rust or JVM)
│   ├── tick_loop                   #   Publishes tick N, waits for all acks, advances
│   ├── process_registry            #   Tracks live processes, their timesteps, front times
│   ├── step_dag                    #   Topological sort, layer execution
│   └── topology_resolver           #   Maps port names -> topic partitions
│
├── state/                          # State materializer (JVM KStreams or Rust)
│   ├── store_tree                  #   Hierarchical state backed by KTable/RocksDB
│   ├── updater_registry            #   Built-in updaters (accumulate, set, merge, ...)
│   ├── view_builder                #   Builds topology views for process input
│   └── structural_ops              #   _add, _delete, _move, _divide handlers
│
├── process-sdk/                    # Per-language SDKs
│   ├── python/
│   │   ├── process_base.py         #   ABC matching current Process/Step interface
│   │   ├── kafka_bridge.py         #   Consume tick, produce update, ack
│   │   └── compat.py              #   Adapter: run existing vivarium Process unchanged
│   ├── rust/
│   │   ├── process_trait.rs
│   │   └── kafka_bridge.rs
│   └── jvm/
│       ├── ProcessInterface.kt
│       └── KafkaBridge.kt
│
├── emitters/                       # Kafka Connect sink configs
│   ├── mongodb-sink.json
│   ├── s3-sink.json
│   └── ram-sink/                   #   In-process consumer for tests
│
├── cli/                            # Simulation launcher
│   ├── run.py                      #   Parse composite, create topics, start coordinator
│   └── inspect.py                  #   Query state topics, replay ticks
│
└── tests/                          # (see Section 7)
```

### 4.2 Module Responsibilities

| Module | Current Vivarium Equivalent | Key Change |
|---|---|---|
| `proto/` | `types.py` + `serialize.py` | Schema-first; language-neutral wire format |
| `core/` | `process.py` (ABC) | Trait/interface definitions only; no implementation |
| `coordinator/` | `engine.py` (Engine class) | Decoupled from state; communicates via Kafka topics |
| `state/` | `store.py` + `registry.py` (updaters) | Persistent, partitioned, queryable state store |
| `process-sdk/python/` | `process.py` + `composer.py` | Thin wrapper; existing processes run with `compat.py` adapter |
| `process-sdk/rust/` | N/A (new) | For computationally intensive processes |
| `emitters/` | `emitter.py` | Declarative sink configs instead of custom classes |
| `cli/` | `engine.py` constructor + `composition.py` | Orchestration layer for topic provisioning and process launch |

### 4.3 Proto Schema (Sketch)

```protobuf
// state.proto
message StoreLeaf {
  string path = 1;
  bytes value = 2;                    // serialized leaf value
  string updater = 3;                 // "accumulate", "set", etc.
  string divider = 4;
  bool emit = 5;
  string serializer = 6;
  map<string, string> properties = 7;
}

// update.proto
message ProcessUpdate {
  string process_path = 1;
  uint64 tick = 2;
  double timestep = 3;
  map<string, bytes> port_updates = 4; // port_name -> serialized update
}

message StructuralUpdate {
  oneof op {
    AddOp add = 1;
    DeleteOp delete = 2;
    MoveOp move = 3;
    DivideOp divide = 4;
    GenerateOp generate = 5;
  }
}

// tick.proto
message TickBarrier {
  uint64 tick = 1;
  double global_time = 2;
}

message TickAck {
  string process_path = 1;
  uint64 tick = 2;
}
```

---

## 5. Feature Parity Matrix

| Feature | Current | Kafka Reimpl | Complexity | Notes |
|---|---|---|---|---|
| Basic process execution | `next_update` | Stream processor per process | Low | Direct mapping |
| Variable timesteps | `calculate_timestep` per process | Coordinator tracks per-process front | Medium | Must handle non-uniform tick requests |
| Conditional execution | `update_condition` | Process sends no-op ack | Low | |
| Hierarchical stores | `Store` tree with `inner`/`outer` | KTable with path-prefix keys | Medium | Lose pointer-based navigation; gain persistence |
| Topology wiring | `inverse_topology` | Topic routing config + view builder | Medium | Must replicate path resolution logic |
| Schema-driven updaters | Registry lookup per leaf | Aggregator per key prefix | Low | |
| Structural mutations (`_add`, `_delete`, `_move`, `_divide`) | `Store.apply_update` special keys | `StructuralUpdate` messages + state materializer | High | Divide = create new partitions/keys + state copy |
| Parallel processes | `multiprocessing.Pipe` | Kafka partition parallelism | Low | **Simpler** than current -- Kafka handles it |
| Step DAG execution | `_StepGraph` + NetworkX | Coordinator runs steps in-order between ticks | Medium | Steps are synchronous barriers |
| Glob schemas (`*`, `**`) | `Store.schema_topology` | View builder must resolve wildcards against live keys | Medium | |
| Emitters (RAM, MongoDB) | Custom `Emitter` subclasses | Kafka Connect sinks | Low | **Simpler** -- declarative config |
| Units (Pint) | In-process Pint quantities | Serialize to magnitude + unit string | Low | Conversion stays in process code |
| Profiling | cProfile stats on Engine | Kafka consumer lag + custom metrics | Medium | Different paradigm; Prometheus-based |
| Composer / MetaComposer | Generates Composite dict | CLI reads Composite, provisions topics | Low | Composers stay in Python |
| Dynamic process generation (`_generate`) | Runtime add to store tree | Command topic + coordinator provisions new consumer | High | Hot-add of processes is complex |

---

## 6. Gaps & Risks

### 6.1 Hard Problems

| Gap | Description | Mitigation |
|---|---|---|
| **Deterministic replay** | Current engine is deterministic (same inputs = same outputs). Kafka consumer ordering within a partition is guaranteed, but cross-partition ordering is not. | Single-partition per simulation for small models; vector clocks for large ones. |
| **Sub-millisecond ticks** | Kafka round-trip latency (~1-5ms) exceeds vivarium's in-memory tick speed (~microseconds for toy models). | Batch ticks: process N ticks locally, emit checkpoints to Kafka every M ticks. Hybrid mode. |
| **Structural mutations at scale** | `_divide` creates new agents with full state copies. In Kafka, this means new keys + potentially new consumer instances. | Pre-allocate agent key space; use compacted topic for agent registry. |
| **Glob topology resolution** | `*` and `**` in schemas require scanning live state keys. KTable doesn't support prefix-scan natively in all clients. | Maintain a metadata topic of active keys; view builder subscribes to it. |
| **Backward compatibility** | Existing `Process` subclasses assume synchronous `next_update` call with direct dict return. | `compat.py` adapter wraps existing processes: deserialize input from Kafka, call `next_update`, serialize output back. Zero changes to process code. |
| **Pint / numpy in wire format** | Protobuf doesn't natively handle numpy arrays or Pint units. | Standardize on magnitude + unit string for Pint; raw bytes + shape metadata for numpy. Or use MessagePack/CBOR instead of Protobuf. |
| **Multi-timescale efficiency** | Current engine only advances to the next scheduled process. With Kafka, idle processes still receive tick barriers. | Coordinator skips processes whose `front.time > global_time`; only sends barriers to due processes. |

### 6.2 What You Lose

- **Simplicity**: `pip install vivarium-core` + 20-line script runs a simulation. Kafka requires broker infrastructure.
- **Debuggability**: Step-through debugging of `next_update` is trivial today. With Kafka, you debug distributed consumers.
- **Latency for small models**: In-memory engine is orders of magnitude faster for <100 processes.

### 6.3 What You Gain

- **Horizontal scale**: 10,000+ agent simulations across a cluster.
- **Fault tolerance**: Process crash = consumer rebalance; state survives in topics.
- **Polyglot processes**: Write a process in any language with a Kafka client.
- **Event sourcing for free**: Full replay, time-travel debugging, audit trail.
- **Decoupled deployment**: Update one process without restarting the simulation.
- **Observability**: Consumer lag, throughput metrics, dead-letter queues.

---

## 7. Test Strategy

### 7.1 Test Tiers

```
Tier 1: Unit Tests (per module, no Kafka)
Tier 2: Integration Tests (embedded Kafka, single node)
Tier 3: Compatibility Tests (vivarium-core parity)
Tier 4: Performance / Scale Tests (multi-node)
```

### 7.2 Tier 1 -- Unit Tests

| Module | Test Focus | Approach |
|---|---|---|
| `proto/` | Schema round-trip: serialize -> deserialize = identity | Property-based tests (Hypothesis / proptest) |
| `coordinator/tick_loop` | Tick advancement logic, front tracking, step DAG ordering | In-memory mock topics (no Kafka) |
| `coordinator/step_dag` | Topological sort, layer generation, removal cascades | Port existing `TestStepGraph` tests directly |
| `state/store_tree` | Hierarchical get/set, path resolution, updater application | In-memory KV store mock |
| `state/updater_registry` | Each built-in updater: accumulate, set, merge, nonneg, null, dictionary | Direct function tests; port from current `registry.py` |
| `state/structural_ops` | `_add`, `_delete`, `_move`, `_divide` on mock store | Port from `test_add_delete`, `test_move_update`, `test_custom_divider` |
| `process-sdk/python/compat` | Existing `Process` subclass runs through adapter unchanged | Instantiate `ToyTransport`, `Proton`, etc.; validate `next_update` output |
| `process-sdk/python/kafka_bridge` | Serialization of states/updates to/from proto | Round-trip with known payloads |

### 7.3 Tier 2 -- Integration Tests (Embedded Kafka)

Port the 23 engine tests from `vivarium/experiments/engine_tests.py`:

| Original Test | Kafka Test | Validates |
|---|---|---|
| `test_recursive_store` | `test_hierarchical_state_topics` | Nested store paths materialize correctly in KTable |
| `test_topology_ports` | `test_topic_routing` | Process reads correct state from routed topics |
| `test_timescales` | `test_multi_timestep_coordination` | Coordinator handles 0.3s and 3.0s processes correctly |
| `test_parallel` | `test_partition_parallelism` | Two processes on separate partitions produce correct output |
| `test_complex_topology` | `test_complex_topic_wiring` | PoQo-equivalent with path remapping through topics |
| `test_units` | `test_unit_serialization_across_wire` | Pint quantities survive Kafka round-trip |
| `test_custom_divider` | `test_division_via_command_topic` | Division creates new agent keys + state copies |
| `test_runtime_order` | `test_step_dag_barrier_sync` | Steps execute in correct order between ticks |
| `test_glob_schema` | `test_wildcard_view_resolution` | `*` schemas resolve against live KTable keys |
| `test_add_delete` | `test_structural_mutations_via_kafka` | Add/delete cycle via command topic |
| `test_hyperdivision` | `test_scale_100_agents` | 100 agents divide correctly; measure throughput |
| `test_engine_run_for` | `test_incremental_ticks` | Partial simulation runs, resume from checkpoint |
| `test_floating_point_timesteps` | `test_time_precision` | No drift in float-based tick advancement |
| `test_move_update` | `test_agent_migration` | Agents move between compartment topics with state |
| `test_emit_config` | `test_selective_emission_to_sink` | Only `_emit=True` fields reach sink topic |
| `test_output_port` | `test_output_port_masking` | Output-only ports excluded from process input views |
| `test_set_branch_emit` | `test_dynamic_emit_control` | Runtime schema overrides propagate to emitter |
| `test_environment_view_with_division` | `test_dynamic_agent_visibility` | Environment process sees newly divided agents on next tick |
| `test_2_store_1_port` | `test_split_port_topic_join` | Port reads from two topics joined by view builder |
| `test_multi_port_merge` | `test_merged_port_topic` | Multiple processes write to same topic key |

### 7.4 Tier 3 -- Compatibility Tests

**Golden-output comparison**: Run identical Composite definitions through both the original `Engine` and the Kafka-backed engine. Assert that emitted timeseries are identical (within float tolerance).

```python
@pytest.mark.parametrize("composite_factory", [
    ToyCompartment,
    PoQo,
    ToyDivider,
    MergePort,
    SplitPort,
])
def test_kafka_matches_original(composite_factory):
    config = {...}
    duration = 10.0

    # Original
    composite = composite_factory(config).generate()
    engine_orig = Engine(composite=composite, emitter='timeseries')
    engine_orig.update(duration)
    original_ts = engine_orig.emitter.get_timeseries()

    # Kafka-backed
    engine_kafka = KafkaEngine(composite=composite, emitter='ram-sink')
    engine_kafka.update(duration)
    kafka_ts = engine_kafka.emitter.get_timeseries()

    assert_timeseries_equal(original_ts, kafka_ts, rtol=1e-10)
```

### 7.5 Tier 4 -- Performance Tests

| Test | Metric | Target |
|---|---|---|
| Single process throughput | Ticks/second | >1000 for trivial process |
| 100-agent division | Wall time for 100 ticks | <10s (current: ~2s in-memory) |
| 1000-agent steady-state | Sustained ticks/second | >100 |
| State checkpoint + restore | Time to resume from topic | <5s for 10k-key state |
| Cross-language round-trip | Python process + Rust process | <5ms overhead per tick vs same-language |

---

## 8. Migration Path

### Phase 0: Schema & SDK (2 weeks)

- Define Protobuf/Avro schemas for `state`, `update`, `tick`, `command`.
- Build `process-sdk/python` with `compat.py` adapter.
- Unit tests (Tier 1) for serialization and adapter.
- **Deliverable**: Existing `Process` subclasses can serialize/deserialize through the new wire format.

### Phase 1: Coordinator MVP (3 weeks)

- Implement `coordinator/tick_loop` with embedded Kafka (Testcontainers or Redpanda).
- Implement `state/store_tree` backed by a single compacted topic.
- Single-partition, single-node only.
- Port `test_topology_ports`, `test_timescales`, `test_runtime_order`.
- **Deliverable**: `ToyCompartment` runs end-to-end through Kafka.

### Phase 2: Structural Mutations (2 weeks)

- Implement `_add`, `_delete`, `_move`, `_divide` via command topic.
- Port `test_add_delete`, `test_custom_divider`, `test_move_update`, `test_hyperdivision`.
- **Deliverable**: Division and dynamic topology work through Kafka.

### Phase 3: Multi-language Process (2 weeks)

- Implement `process-sdk/rust` with `kafka_bridge.rs`.
- Rewrite one toy process (e.g., `ToyMetabolism`) in Rust.
- Run mixed Python + Rust simulation.
- **Deliverable**: Polyglot process execution proven.

### Phase 4: Emitters & Observability (1 week)

- Configure Kafka Connect sinks for MongoDB and S3.
- Add Prometheus metrics to coordinator.
- Port `test_emit_config`, `test_set_branch_emit`.
- **Deliverable**: Data pipeline matches current emitter capabilities.

### Phase 5: Scale & Harden (2 weeks)

- Multi-partition support for agent-parallel simulations.
- Checkpoint/restore from topic offsets.
- Tier 3 compatibility tests (golden-output comparison).
- Tier 4 performance benchmarks.
- **Deliverable**: Production-ready for large-scale simulations.

### Phase 6: Deprecation Bridge (ongoing)

- `KafkaEngine` as drop-in replacement for `Engine`:
  ```python
  # Only change: import
  from vivarium.kafka import KafkaEngine as Engine
  engine = Engine(composite=composite, emitter='timeseries')
  engine.update(100)
  ```
- Maintain `Engine` for local/fast/debug use; `KafkaEngine` for distributed/durable/scale.

---

## Appendix A: Decision Log

| Decision | Rationale |
|---|---|
| Coordinator pattern over barrier-free | Preserves determinism; simplest first step |
| Protobuf over Avro | Stronger cross-language codegen; Avro better for schema evolution but less ergonomic in Rust |
| JVM for state materializer (optional) | Kafka Streams is JVM-native; if Rust is preferred, use rdkafka + custom RocksDB |
| Keep Composers in Python | They are build-time factories, not runtime-hot; biologist ergonomics matter |
| `compat.py` adapter over rewrite | Hundreds of existing `Process` subclasses in the ecosystem must keep working |
| Single-partition MVP | Avoids distributed coordination complexity in early phases |
| Redpanda for dev/test | API-compatible with Kafka, single binary, faster startup for CI |

## Appendix B: Dependency Comparison

| Current (Python) | Kafka Stack |
|---|---|
| `networkx` | Coordinator embeds DAG sort (or use petgraph in Rust) |
| `numpy` / `scipy` | Stay in process code (Python or Rust ndarray) |
| `Pint` | Serialize as `{magnitude, unit_string}`; reconstruct in process |
| `pymongo` | Kafka Connect MongoDB sink |
| `orjson` | Protobuf / MessagePack on wire; orjson within Python SDK |
| `matplotlib` | Unchanged; post-hoc analysis tool |
| `multiprocessing` | **Eliminated** -- Kafka handles distribution |

## Appendix C: Glossary

| Term | Definition |
|---|---|
| **Front** | Per-process dict `{time, update}` tracking when each process next completes |
| **Topology view** | Filtered snapshot of state matching a process's declared ports |
| **Inverse topology** | Transform from port-relative update paths to absolute store paths |
| **Compacted topic** | Kafka topic where only the latest value per key is retained |
| **KTable** | Kafka Streams abstraction: changelog stream materialized as a table |
| **Punctuator** | Kafka Streams callback triggered by wall-clock or stream-time |
| **Consumer lag** | Difference between latest produced offset and consumer's current offset |
