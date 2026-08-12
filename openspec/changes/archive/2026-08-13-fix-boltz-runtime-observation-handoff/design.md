## Context

See `proposal.md` for motivation. `validate_boltz_runtime()` already owns the pinned Boltz version and checkpoint validation and returns both observations. `_prepare_boltz_environment()` currently discards that return value and references two names that do not exist in its scope, so a successful preflight raises `NameError` before the external Boltz command runs. `run_boltz_prediction()` already consumes the prepared environment and publishes the same fields through its existing result schema.

The implementation must remain inside `prediction_pipeline/boltz_worker.py`; no neighboring workflow owner or scientific contract needs a change.

## Goals / Non-Goals

**Goals:**

- Make the validator return value the single data-flow source for the existing prepared-environment runtime fields.
- Prove the handoff at both preparation and public scientific execution seams.
- Keep one small enrichment-boundary test to show the corrected result reaches the next existing seam.

**Non-Goals:**

- No retry or resume repair.
- No promotion or recovery of the nine already-generated base bundles.
- No readiness, budget, execution-identity, transaction, Store, or scientific-protocol changes.
- No new runtime identity type or generalized outcome framework.

## Decisions

### Capture and forward the validator result

`_prepare_boltz_environment()` will retain the mapping returned by `validate_boltz_runtime()` and populate its existing `version` and `checkpoint_sha` entries from `version` and `checkpoint_sha256` respectively. This keeps validation ownership in one place and avoids recomputing or hardcoding observations.

Alternative considered: independently re-read package metadata and checkpoint content during preparation. Rejected because it duplicates the validator contract and can produce identity drift between validation and publication.

### Test two merge-blocking seams and one compact downstream boundary

The first regression directly characterizes preparation with only the validator stubbed. The second crosses public `run_boltz_prediction()` while stubbing the external process/output boundary, proving final metadata and command inputs. The third uses a small enrichment fixture and stops at the next existing scientific seam; it is supporting coverage rather than a second end-to-end harness.

Alternative considered: a large Worker/Store fixture. Rejected because it would duplicate orchestration coverage and expand the test beyond the defective data-flow boundary.

### Preserve all existing interfaces and policy owners

No signature, result schema, protocol value, readiness rule, retry state, or persistence behavior changes. This is a value-source correction within an existing adapter.

## Risks / Trade-offs

- [Risk] A test could pass by replacing the preparation function and miss the defect. → The two merge-blocking regressions keep real preparation in the call path.
- [Risk] The enrichment regression could become an expensive orchestration fixture. → Stop it at the next scientific seam and reuse minimal values only.
- [Risk] Separate budget and retry defects remain visible after this fix. → Keep them explicit non-goals for independent governed changes.

## Migration Plan

Deploy the single adapter wiring change with its regressions. Existing artifacts and failed invocations are not rewritten. Rollback is the normal revert of this change; no data migration is required.
