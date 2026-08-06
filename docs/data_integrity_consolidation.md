# Data Integrity Consolidation

## Scope

This consolidation keeps the current Contract/Trace layer, ProjectContext
injection points, and modular Design package. It changes persistence and
Execution commit boundaries only; scientific algorithms, protocols, thresholds,
and route selection are unchanged.

## Before

```text
Agent
  -> State JSON / Candidate CSV / Evidence JSONL
  -> optional SQLite backend

Execution handler
  -> formal files may change during the handler
  -> Orchestrator completion
```

The file backend was the runtime default, Candidate IDs were derived with a
read/scan/write sequence, and SQLite and CSV could disagree.

## After

```text
State / CandidateIndex / EvidenceLogger compatibility API
  -> SQLite store.db (formal source of truth)
  -> one-way JSON / CSV / JSONL projections

Execution
  -> TransactionContext
  -> Adapter
  -> isolated StagingArea
  -> validation
  -> CommitManager
  -> SQLite atomic commit + artifact publish
  -> Orchestrator completion validation
```

Candidate IDs are reserved by a database sequence under an immediate write
transaction. Failed scientific work may leave an unused ID, but cannot collide
with another worker and does not publish a candidate row or registration event.

## Migration sources

- From `fix/p0-data-integrity`: SQLite authority, WAL/busy timeout, immediate
  write transactions, database Candidate ID sequence, and projection semantics.
- From PR37: TransactionContext, ExecutionActionResult, Adapter, StagingArea,
  ExecutionWorker, CommitManager, and Recovery boundaries.
- Kept from current `chemzi/dev`: Contract/Trace, ProjectContext support,
  Execution action registry, and modular `agents/design/` implementation.

The old standalone `store.py` and monolithic Design implementation were not
copied. Legacy files are imported only through an explicit migration call.
Editing a projection never changes formal state.

## Correctness decisions

- Each Design job emits its own CandidateUpdate batch, so later jobs cannot
  overwrite earlier results.
- Empty CandidateUpdate batches are valid.
- Artifact paths are isolated by artifact ID, so equal basenames do not collide.
- Candidate registration evidence is inserted in the same database transaction
  as the candidate rows and therefore cannot precede commit.
- Legacy handlers still pass the normal Orchestrator output validation.
- If Orchestrator completion fails after effect commit, the worker compensates
  the database rows, evidence, state patch, and published artifacts before the
  task is marked failed. A retry can then publish once.
- Recovery markers distinguish prepared filesystem moves from committed store
  registrations after a process crash.

## Explicit legacy migration

Use `data_layer.migrate_legacy_data(...)` for the compatibility schema-aware
path, or `storage.migrate_json_to_sqlite(...)` for generic records. Source files
are preserved and then replaced only when the configured projection is rebuilt.
There is no timestamp-based or automatic reverse synchronization.

## Remaining debt

- Prediction, critic, and calibration handlers still use the legacy adapter.
  Their outputs are validated, but their domain-specific formal mutations should
  move into typed transaction results in follow-up changes.
- Projection refresh is synchronous and rewrites the complete human-readable
  view. This is deliberate for correctness; large deployments may later use a
  durable projection cursor without changing database authority.
- The repository's legacy test loader mutates module imports during discovery;
  tests should continue running per module until that test harness is isolated.
