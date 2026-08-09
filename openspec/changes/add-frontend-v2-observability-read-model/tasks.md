## 1. Characterize the Current Read Authorities

- [x] 1.1 Add focused characterization fixtures covering a Store-backed current project with no run, a validated Orchestrator run with non-linear task dependencies, an unavailable action, and a failed task.
- [x] 1.2 Add transaction and provenance fixtures covering committed, failed, rolled-back, compensation-conflict or unresolved records, plus a claimed task whose transaction is not yet formally recorded.
- [x] 1.3 Preserve focused tests for every currently supported `/api/v1` route so the additive v2 work cannot silently change existing methods, envelopes, or behavior.

## 2. Add Narrow Store Read Support

- [x] 2.1 Extend the existing storage interfaces with project-scoped artifact metadata and transaction record/list reads, without adding writes or changing the SQLite schema.
- [x] 2.2 Implement the reads in `SQLiteStore` with deterministic ordering and domain-shaped mappings, preserving current transaction status and provenance fields.
- [x] 2.3 Add storage tests proving project isolation, filtering by workflow/run/task/attempt where supported, status preservation, and absence of write-path or projection dependencies.

## 3. Build the Browser Observability Read Model

- [x] 3.1 Introduce a dedicated read-model module that resolves the current Store-backed project and validates the current run through the public Orchestrator status and Plan contract seams.
- [x] 3.2 Join Planner task definitions, Orchestrator task state, Action Catalog metadata, and Action Registry availability into graph-shaped task/action views with stable blocker codes.
- [x] 3.3 Join Store-backed candidates, evidence, artifacts, and recorded transactions to workflow/run/task/attempt trace identifiers, representing missing transaction records as `not_yet_recorded` rather than inferred state.
- [x] 3.4 Sanitize every browser view so internal database, project, plan, run, dispatch, staging, artifact, task-directory, and SSH workspace paths are not serialized.
- [x] 3.5 Preserve producing protocol identities and artifact/evidence provenance without relabeling historical records or assuming a fixed Agent sequence.
- [x] 3.6 Add focused read-model tests for no-run, ready, awaiting-approval, dependency-blocked, unavailable-action, failed-task, committed-transaction, unresolved-recovery, collection scope and `total`/`returned`/`truncated` semantics, the specified trustworthy partial response for invalid run/plan binding, and the allowlisted `exploration_shortlist` evidence payload.

## 4. Expose and Document the V2 Read Interface

- [x] 4.1 Add `GET /api/v2/workbench` using the existing success/error envelope with a versioned, read-only response and stable integrity error codes.
- [x] 4.2 Add HTTP contract tests proving opaque identifiers, non-linear task representation, truthful blocker/failure states, formal Store authority, forbidden-path redaction, the complete read-only/no-side-effect invariant, and the absence of v2 mutation routes.
- [x] 4.3 Update frontend-facing API and Web GUI documentation only where needed to describe the current execution model, the v2 read interface, v1 compatibility status, and the prohibition on projection/log-derived workflow state.
- [x] 4.4 Confirm `web-gui/` production code remains unchanged in this backend-first change and record PR #26 solely as UX/product reference, not an implementation dependency.

## 5. Verify the Change

- [x] 5.1 Run the focused Web API, storage, Planner, Orchestrator, Execution, transaction/recovery, trace, protocol, and ProjectContext test modules.
- [x] 5.2 Run the full CPU test suite and confirm existing CLI, public contracts, persistence formats, transaction behavior, and scientific behavior remain compatible.
- [x] 5.3 Run the Architecture Gate, applicable lint/type/static checks, strict OpenSpec validation, OpenSpec implementation verification, and strict code review.
