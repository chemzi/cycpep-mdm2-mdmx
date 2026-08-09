## Why

The current browser adapter exposes a legacy snapshot centered on `State.phase`, candidate projections, and recent evidence, while the implemented backend now executes typed Planner tasks through Orchestrator, Action Registry, ExecutionWorker, transactional Store commits, and formal trace contracts. Frontend V2 needs one truthful, read-only interface over those current authorities before any new UI can safely present workflow progress.

## What Changes

- Add a versioned, browser-facing observability read model for the current project and its current workflow/run, including task and typed-action availability, execution/transaction status, candidates, evidence, artifacts, protocol provenance, trace identifiers, and explicit blockers.
- Add a minimal read-only HTTP endpoint that returns this read model through opaque identifiers and stable error/status codes without exposing server paths or raw persistence records.
- Add the smallest Store read operations needed to query formal artifact and transaction metadata through the existing Store seam; SQLite remains the authority and JSON/CSV/JSONL files remain one-way compatibility projections.
- Derive task and run status from the public Orchestrator status interface, executable capability from the canonical Action Catalog and Action Registry, and scientific provenance from existing protocol and trace contracts. The adapter will not infer workflow state from `State.phase`, log text, or a fixed Agent sequence.
- Preserve all existing `/api/v1` routes as compatibility interfaces. Frontend V2 will not treat `/api/v1/snapshot` as its workflow authority.
- Add focused characterization and contract tests plus synchronized frontend-facing documentation for the new read model.
- Leave target-draft creation and approval on their existing routes. Missing bootstrap mutations and all execution controls remain deferred.

No existing public interface or persisted data format is removed or changed. The new HTTP contract is additive. PR #26 remains a UX reference only; none of its controller, workflow-state, API, or backend assumptions are imported.

## Capabilities

### New Capabilities

- `frontend/browser-observability`: Provides a truthful, read-only browser interface over the current Project → Workflow/Run → Task → typed Action → Execution/Transaction → Store → Candidate/Evidence/Artifact/Trace model.

### Modified Capabilities

None.

## Impact

- Affected areas: `web_api/`, the existing storage read interfaces and SQLite adapter, focused Web API/storage tests, and frontend-facing API documentation.
- Public API: additive versioned read-only endpoint and response model; existing `/api/v1` behavior remains compatible.
- Persistence: no schema migration and no change to write authority, transaction ownership, staging, commit, compensation, or recovery semantics.
- Frontend: no UI implementation in this change; `web-gui/` is used only as characterization evidence for the later Frontend V2 presentation change.
- Dependencies: no new runtime framework, dashboard database, queue, scheduler, SSH controller, or direct browser access to SQLite or filesystem paths.
- Deferred legacy paths: `/api/v1/snapshot`, the current fixed four-stage GUI presentation, incomplete target-bootstrap routes, and the legacy SSH snapshot remain available but are not extended or adopted as the Frontend V2 execution model.
