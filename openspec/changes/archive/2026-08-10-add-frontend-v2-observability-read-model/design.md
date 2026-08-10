## Context

See `proposal.md` for motivation. The current backend is no longer the linear snapshot described by the older frontend documents:

- `agents/planner/` produces an immutable plan of typed tasks whose readiness is constrained by `contracts.action.ACTION_CATALOG` and the registered handlers in `execution/action_registry.py`.
- `agents/orchestrator.status()` revalidates a run against its bound plan and derives run/task states. Orchestrator still persists its detailed run artifact separately and projects only a summary into State; changing that authority is intentionally deferred.
- `execution/worker.py` claims a task, validates its dispatch packet, invokes the registered typed adapter, stages effects, commits through `CommitManager` and the Store, closes Orchestrator, and writes a receipt. Recovery and compensation can leave explicit unresolved states.
- `data_layer.get_storage_backend()` returns the project-scoped `SQLiteStore`; State, CandidateIndex, and EvidenceLogger use it and only then rebuild JSON/CSV/JSONL projections.
- `SQLiteStore` can list candidates and query evidence but exposes only single-artifact and single-transaction-status reads. A truthful aggregate view therefore needs small read-only additions to the existing Store seam.
- `web_api/server.py` currently offers `/api/v1/snapshot`, target-draft routes, ephemeral coordinate registration, and a legacy read-only SSH snapshot. `web-gui/app/page.tsx` hard-codes four Agent stages and treats `State.phase` plus evidence counts as workflow presentation state.
- `docs/frontend_api_contract.md` still specifies independent Research/Design run resources and a frontend state machine; `docs/web_gui_implementation.md` still describes JSON/CSV/JSONL files as UI sources. These are not the current execution model.

Current characterization evidence includes `test_planner.py`, `test_orchestrator.py`, `test_execution.py`, `test_data_integrity_transactions.py`, `test_recovery_hardening.py`, `test_storage.py`, `test_protocol.py`, `test_project_context.py`, and `test_web_api.py`. PR #26 is 84 commits behind the current integration and introduces a separate automatic controller, so only its workbench, project context, candidate, structure, evidence, artifact, and observability ideas are relevant.

## Goals / Non-Goals

**Goals:**

- Establish one deep browser-observability module whose small interface returns a complete, sanitized workbench view.
- Preserve dependency direction: HTTP adapter → read-model module → public contracts/Store/Orchestrator status; never browser → persistence or handler.
- Represent the task graph and action availability as data, so future actions such as calibration, Tournament, Pareto exploration, and adaptive design do not require a new frontend state machine.
- Make missing or not-yet-formalized information explicit rather than filling it from projections or logs.

**Non-Goals:**

- Frontend rendering, navigation, visual redesign, or PR #26 code reuse.
- Start, approve, retry, cancel, claim, dispatch, scheduler, GPU, or SSH-control endpoints.
- New project creation flows or completing the missing target-bootstrap routes.
- Moving Orchestrator run authority into SQLite, changing transaction lifecycle events, or adding staging records merely for the UI.
- Persisting a dashboard database, adding SSE/WebSocket transport, or exposing all historical projects/runs.
- Threshold changes, scientific calibration, sample ingestion, Tournament/Pareto selection, or algorithm changes.

## Decisions

### 1. Add one aggregate Frontend V2 workbench endpoint

Add `GET /api/v2/workbench` with a top-level schema version and these stable sections: `project`, `workflow`, `run`, `tasks`, `executions`, `transactions`, `candidates`, `evidence`, `artifacts`, `protocols`, `trace`, and `blockers`. Collections may be bounded for the first slice, but truncation and counts must be explicit.

`project` always describes the current project. `workflow`, `run`, `tasks`, `executions`, and `transactions` describe only the validated current workflow/run. `candidates`, `evidence`, and `artifacts` are project-scoped so useful scientific history remains visible, but each item preserves its available formal trace identifiers and declares whether it belongs to the current run, a historical run, or cannot be linked to a run. Every bounded collection uses the same envelope: `total` is the number of matching formal records before the response limit, `returned` is the number in `items`, and `truncated` is true exactly when `returned < total`.

This is a deep module interface: the browser learns one read contract while the implementation absorbs joins across current authorities. Separate resource endpoints were considered, but they would multiply interface surface before pagination, history, and multi-project selection requirements are known. Extending `/api/v1/snapshot` was rejected because its shape encodes `State.phase`, seven-layer candidate presentation, and raw recent-evidence assumptions that Frontend V2 must not inherit.

### 2. Assemble state from existing authoritative seams

The read-model implementation will:

1. resolve the current project and current Orchestrator summary through the Store-backed State interface;
2. call the public Orchestrator status interface when a current run exists, preserving its plan/run validation;
3. load the already bound immutable plan internally and validate it through the public Plan contract before joining task definitions to run task state;
4. join each task action to Action Catalog metadata and actual Action Registry handler availability;
5. query candidates, evidence, artifacts, and transactions through the project Store;
6. project protocol bindings and trace identifiers already carried by tasks, artifacts, candidates, and evidence.

The browser never receives the run path, plan path, dispatch path, task directory, artifact path, database path, or SSH workspace path. Calling private Orchestrator helpers was considered and rejected; the implementation must remain on the public status and contract seams.

### 3. Extend the existing Store seam only for read access

Add narrow, project-scoped reads for artifact metadata and transaction records/listing to the existing storage interfaces and `SQLiteStore`. Returned values are domain-shaped mappings suitable for further sanitization, not SQL rows. No schema change or write-path change is needed.

The new interface methods have compatibility defaults so existing third-party or test Store implementations remain constructible, while `SQLiteStore` provides the production implementation. The methods remain colocated in the existing SQLite adapter for this narrow change even though that file is already large: extracting persistence modules would be a separate structural refactor, is explicitly deprioritized for the terminal sprint, and would expand this browser-read vertical slice beyond its rollback boundary.

Project-scoped reads rely only on project identity already declared by formal records. This change does not add general Store write validation or alter existing write compatibility; schemas and transaction ownership remain unchanged.

Direct SQL in `web_api` was rejected because it would couple the browser adapter to SQLite layout. Reading compatibility projections was rejected because it would recreate multiple authorities. A new dashboard repository was rejected because it would be a second workflow state store.

### 4. Treat transaction observability as eventually formal, not guessed

Committed, failed, rolled-back, compensation-conflict, and unresolved transaction states come from Store records and contract-bound evidence. Before a record exists, a claimed task reports execution progress from Orchestrator/task evidence but transaction visibility is `not_yet_recorded`; the adapter does not inspect `.staging`, `execution_started.json`, worker processes, or log text.

Adding a new durable CREATED/STAGING transaction write was considered and rejected because it changes transaction semantics and recovery ownership for a UI need. That can be a later independently justified change.

### 5. Keep action availability and blockers structured

Each task view carries its opaque task ID, dependency IDs, disposition, task status, action name, resource class, catalog executability, registry availability, execution-gate state, approval state, and reason codes. Browser presentation is a graph/list driven by these fields, not by a hard-coded Agent sequence. Failures and integrity problems use stable codes plus display-safe summaries.

Unknown future action values must remain renderable as unavailable data rather than crashing the response. This does not weaken the backend closed world: only cataloged and registered actions can be executable.

### 6. Sanitize artifacts while preserving provenance

Artifact views expose opaque ID, type/role, integrity identity required by the existing contract, producer task/attempt, schema version, input artifact IDs, and trace links. Internal paths are always removed. The existing `/api/v1/artifacts/{artifact_id}/coordinates` route remains the only coordinate-content compatibility path in this change; general artifact content serving is deferred.

Protocol identity is copied from the producing record. The read model must not relabel historical evidence with the currently active protocol. This leaves clean extension points for threshold calibration cohorts, real-sample provenance, Tournament/Pareto decision evidence, and demo presentation without defining those capabilities now.

Evidence payload remains allowlisted rather than generically serialized. For the established `exploration_shortlist` event contract only, the browser view preserves `k`, `n_evaluated`, `n_passed`, `shortlist`, `calibration`, `source_event_ids`, and the additive `unmapped_metrics` field. Other event types do not inherit this payload surface.

### 7. Preserve v1 and isolate rollout

No existing `/api/v1` route changes semantics. Frontend V2 will later switch its read path to `/api/v2/workbench` in a separate approved change. Rollback consists of removing the additive v2 route/read-model module and Store read methods; all existing runtime, persistence, and v1 behavior remains available.

### 8. Keep observation side-effect free

The read model uses read-only Store methods and already persisted contracts. A request must not initialize missing formal state, refresh a compatibility projection, register an artifact, create evidence, mutate a run or task, or advance, roll back, recover, or otherwise change a transaction. This rules out convenience calls whose read-like names hide initialization or projection writes; tests observe the public endpoint and compare all affected formal records and compatibility outputs before and after the request.

### 9. Return trustworthy partial data for an invalid current binding

An invalid current run/plan/project binding has one browser contract: `GET /api/v2/workbench` returns HTTP 200 in the normal success envelope, retains trustworthy current-project and project-scoped Store collections, sets `workflow` and `run` to null, returns empty current-run `tasks`, `executions`, and `transactions` collections, and includes a structured `workflow_binding_invalid` blocker scoped to `workflow`. It never falls back to `State.phase`, directory discovery, compatibility projections, or log interpretation. A transport-level error envelope is reserved for failures that prevent even the trustworthy project-scoped read model from being assembled.

## Risks / Trade-offs

- **[Current run discovery still depends on the State Orchestrator summary]** → Validate project/run identity and call public Orchestrator status; return a structured integrity failure instead of falling back to phase or scanning directories. Moving run authority is deferred.
- **[A transaction is not observable during the earliest staging window]** → Report `not_yet_recorded` explicitly and do not modify transaction ownership for presentation convenience.
- **[The aggregate response can grow with candidates and evidence]** → Apply explicit first-slice limits and include total/truncated metadata; pagination can be added as a separate compatible capability when real volumes require it.
- **[Legacy v1 snapshot and fixed-stage GUI remain available]** → Document them as compatibility interfaces and prevent Frontend V2 tests from depending on them as workflow authority.
- **[Artifact metadata may contain sensitive internal paths]** → Centralize serialization in the read-model module and contract-test forbidden path fields and path-shaped leakage.
- **[Joining several authorities can yield partial data]** → Distinguish absent optional data from invalid required bindings; fail the workflow portion closed when plan/run validation fails while never inventing state.

## Migration Plan

1. Characterize existing v1 route behavior and current Store/Orchestrator contracts.
2. Add and test the read-only Store queries without changing schema or writers.
3. Build and contract-test the sanitized read-model module against temporary SQLite Stores and validated run fixtures.
4. Add the v2 endpoint and error envelope while preserving all v1 routes.
5. Synchronize frontend API documentation to identify v2 as the execution-observability contract and v1 snapshot as compatibility-only.
6. Run focused tests, the applicable full Python suite, Architecture Gate, strict OpenSpec validation, and review.

Rollback removes the additive v2 endpoint, read-model module, and unused read methods. No data migration or restoration is required.
