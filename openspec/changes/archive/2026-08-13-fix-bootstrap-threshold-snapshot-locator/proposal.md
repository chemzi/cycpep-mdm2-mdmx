## Why

A real merged-E3 Launcher run completed and committed bootstrap Prediction, but E3 publication failed with `thresholds_invalid`. Transaction promotion moved the authoritative handoff to the committed artifact root while leaving its Prediction-time `inputs/thresholds.json` only in attempt staging, so the formal handoff no longer locates the immutable threshold snapshot that its digest binds.

## What Changes

- Promote the Prediction-time threshold snapshot as an additional transaction artifact in the same Prediction transaction as the handoff, without changing the action's sole semantic output role.
- Bind the committed threshold artifact ID and canonical threshold digest in formal `prediction_handoff_ready` Evidence; keep committed path and byte SHA-256 solely on the Store Artifact, then project the validated locator into the bootstrap Prediction boundary.
- Make E3 publication read only that owner-validated formal threshold locator; do not fall back to attempt staging or current State.
- Preserve atomic commit, owner readiness, retry, scientific protocol, and legacy direct-Prediction behavior.

## Capabilities

### New Capabilities

- `workflow/bootstrap-prediction-threshold-snapshot`: Formal bootstrap Prediction completion exposes the committed threshold snapshot required by downstream E3 publication.

### Modified Capabilities

- None.

## Impact

The narrow write set is Prediction transaction effects/promotion, bootstrap Prediction inspection, E3 publication locator consumption, and focused lifecycle regressions. `evaluate_new_design_candidates` continues to expose only `prediction_handoff`; the formal Evidence payload gains one `thresholds_artifact_id`, while the existing Store Artifact row remains the sole owner of committed path and byte SHA-256. The strict Prediction effects contract is revised in place for this internal producer/consumer pair; no public action-output role, Store schema migration, scientific-policy change, readiness relaxation, retry change, or old-run backfill is introduced. Old failed invocations remain immutable and require a fresh run.
