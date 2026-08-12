## Context

See `proposal.md` for motivation and `specs/scientific/calibration-baseline/spec.md` for required behavior.

The existing scientific algorithm is already adequate for E1: `threshold_calibration.py` validates project/protocol-bound control metadata and preserves the current minimum controls, FPR, recall, Wilson interval, separation, and core-metric rules. `prediction_pipeline.protocol.protocol_binding()` is the sole active Prediction protocol identity. The missing engineering chain is that calibration artifact registration, Evidence, and threshold State are separate writes, while Prediction records only a runtime threshold digest and cannot locate or validate the formal calibration that produced it.

The one-day MVP is deliberately limited to three connected boundaries:

A. a versioned `CalibrationBaseline` binding contract;
B. one atomic SQLite publication operation;
C. Prediction validation and recording of the consumed binding.

Synthetic controls are permitted only to prove this engineering lifecycle. The shipped MDM2/MDMX manifest remains unchanged, unscored, provenance-only, and outside the implementation path.

## Goals / Non-Goals

**Goals:**

- Represent `calibration_authority` explicitly as `simulation_only` or `approved_real` and preserve it across dataset, artifact, Store, Evidence, cache, and Prediction records.
- Build a deterministic baseline/artifact/publication identity from canonical scientific and binding content.
- Atomically publish artifact registration, thresholds/binding, and formal Evidence in SQLite with deterministic idempotency.
- Make Prediction fail closed on any project/protocol/authority/dataset/threshold/artifact mismatch and copy the exact consumed binding to all required outputs.
- Prove the complete path using explicit simulation controls and the unchanged calibrator.

**Non-Goals:**

- No calibration algorithm, FPR/recall/sample, metric scope, threshold, or clearance change.
- No real-control research, MDM2/MDMX manifest promotion, project-config migration, scoring-script expansion, or claim of experimentally validated calibration.
- No UI, Launcher, Planner, Execution, Research workflow, Store schema/redesign, dedicated threshold table, or E2+ work.
- No relabelling of historical Prediction records and no `state.json` or environment bypass as authority.

## Decisions

### 1. Keep authority separate from algorithmic calibration status

Add one small public baseline contract module beside `threshold_calibration.py`. The existing calibrator continues to decide per-metric `calibration_status`; the new contract decides the baseline's scientific authority. These are orthogonal:

- `calibration_status=calibrated` means the unchanged statistical contract passed for that dataset;
- `calibration_authority=simulation_only` means the dataset is synthetic/scenario data and the result is not approved real scientific calibration;
- only `calibration_authority=approved_real` may be represented as approved real calibration.

The dataset metadata and every synthetic control record carry an explicit synthetic marker. Building or validating an `approved_real` binding rejects any synthetic marker. Every serialized downstream binding includes the authority field; omission is invalid rather than defaulting to real.

Reuse `prediction_pipeline.protocol.protocol_binding()` and the repository's existing canonical object/file integrity helpers. No new digest algorithm or second protocol identity is introduced.

Alternative considered: overload `calibration_status` with `simulation_calibrated`. Rejected because it would change the existing algorithm/status contract and mix statistical outcome with scientific authority.

### 2. Derive one deterministic natural publication identity

Construct a canonical scientific binding from:

- binding/threshold schema versions;
- calibration authority;
- project ID and approved project digest;
- Prediction protocol and scoring implementation identities;
- scored dataset digest;
- canonical threshold snapshot digest;
- metric status summary.

Runtime timestamps, paths, generated IDs, and Store metadata are excluded. The scientific binding digest deterministically derives `publication_id`; the immutable calibration artifact is canonical content derived from the same binding plus the existing calibrator audit, and its file digest is recorded in the publication binding. Artifact ID is deterministic from the publication ID.

The Store treats `(publication_id, complete binding, artifact digest, threshold snapshot)` as one natural identity:

- an exact replay is an idempotent success and performs no conflicting second activation;
- the same `publication_id` with any different content raises a domain error before commit;
- a caller-supplied ID that does not equal the derived ID is invalid.

Alternative considered: random UUID publication IDs. Rejected because retries would create multiple active identities for identical scientific content and collision semantics would be untestable.

### 3. Publish all formal calibration state in one SQLite transaction

Add one additive Store method and data-layer delegation for calibration publication. Under one existing `BEGIN IMMEDIATE` boundary, SQLite:

1. checks an existing active/same-ID publication for exact idempotency or collision;
2. registers the deterministic calibration artifact row and file digest;
3. replaces formal State `thresholds` and `threshold_calibration_binding` together;
4. appends a validated `threshold_calibration_published` Evidence event carrying the complete binding.

Any exception rolls back all formal writes. The artifact file is written and verified before the transaction; on Store failure it is merely an unregistered input file, never authority. `state.json` refresh happens after commit and remains a projection. No fake Execution transaction or schema redesign is added.

Alternative considered: reuse `commit_transaction` with a synthetic task. Rejected because Research/calibration publication is not an Orchestrator-owned Execution action. Alternative considered: compensate three independent writes. Rejected because partial authority is the defect being fixed.

### 4. Validate at the Prediction entry boundary and propagate unchanged

`agents.prediction.run` loads thresholds and `threshold_calibration_binding` from the same SQLite-backed State. Before constructing the pipeline it validates:

- publication ID matches the canonical binding;
- project ID/approved digest and active `protocol_binding()` match;
- runtime threshold digest matches the binding;
- referenced artifact row exists;
- artifact file content matches both Store and binding digests;
- simulation/real authority is internally consistent with artifact/dataset provenance.

The validated binding is passed explicitly to `PredictionPipeline`, participates in cache identity, and is copied unchanged into run manifest, handoff/summary, candidate record, candidate metadata, and scoring/record Evidence. A missing binding remains compatible only for thresholds that do not claim formal calibration; a stale or partial binding fails closed.

Alternative considered: validate only `thresholds_digest`. Rejected because equal numeric thresholds do not identify authority, dataset, project, protocol, artifact, or publication.

### 5. Use one deterministic simulation lifecycle as the primary acceptance

Focused tests build an explicit `simulation_only` dataset with enough positive/negative values to satisfy the unchanged calibrator. The test then:

1. runs the existing calibrator;
2. creates the canonical simulation-only artifact/binding;
3. publishes it atomically to a temporary SQLite Store;
4. runs Prediction against the Store-owned thresholds/binding;
5. reads back cache/run/record/Evidence identities and confirms dataset/protocol/threshold/artifact/publication traceability.

Companion regressions cover exact replay idempotency, same-ID different-content rejection, synthetic-to-approved-real rejection, partial Store rollback, projection independence, and dataset/protocol/threshold/artifact tamper. The fixture proves engineering behavior only and is not added to repository scientific control assets.

## Risks / Trade-offs

- [Risk] A statistically calibrated simulation could be mistaken for scientific validation. → Keep authority orthogonal, required, and present in every formal/Prediction surface; reject synthetic provenance under `approved_real`.
- [Risk] Artifact files remain external to SQLite. → Register their digest atomically and verify file content before Prediction consumption.
- [Risk] A new Store method expands the backend contract. → Keep it additive and domain-specific with no schema change; SQLite is the only implementation in scope.
- [Risk] Projection refresh can fail after Store commit. → SQLite remains authoritative and the existing projection refresh can repair files without changing the active binding.
- [Risk] Existing Prediction tests construct pipelines without a formal calibration binding. → Preserve compatibility only when their thresholds do not claim formal calibrated authority; add explicit consumed-binding fixtures for E1 behavior.
- [Trade-off] `PredictionPipeline` and `SQLiteStore` are existing large modules at the required public seams. → Keep their E1 edits to explicit binding propagation and one narrow delegation; place SQLite publication mechanics in a typed sibling collaborator. Splitting either established module is deferred because it would expand this one-day interface migration beyond the approved MVP.

## Migration Plan

1. Add the baseline contract and deterministic simulation/idempotency tests.
2. Add the atomic Store publication and rollback/idempotency tests.
3. Add Prediction validation/propagation and the complete simulation lifecycle test.
4. Run focused tests, full Python discovery, configured lint/type checks, Architecture Gate, strict OpenSpec validation/verification, `git diff --check`, and fixed-point Spec/Standards/Strict review.

Rollback is a normal branch revert. No live Launcher run, project configuration, real control asset, or runtime directory is migrated or restarted.
