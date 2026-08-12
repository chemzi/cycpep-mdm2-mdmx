## Why

The production Boltz Worker validates the configured runtime successfully but discards the validator's returned observations, then tries to publish undefined `checkpoint_sha` and `version` names. A real approved Prediction enrichment therefore fails before launching Boltz instead of carrying the validated runtime identity into the artifact bundle.

## What Changes

- Wire the existing `validate_boltz_runtime()` result through Boltz environment preparation and final artifact metadata.
- Add two merge-blocking regressions at the preparation and public `run_boltz_prediction()` seams.
- Add one compact enrichment-boundary regression proving the corrected scientific result can hand off to the next enrichment step.
- Keep retry behavior, base-bundle promotion, Prediction readiness, budgets, and the scientific protocol unchanged.

## Capabilities

### New Capabilities

- `execution/boltz-runtime-observation-handoff`: Defines how successful pinned-runtime observations are carried into one Boltz scientific execution and its artifact metadata.

### Modified Capabilities

None.

## Impact

- Production code: `prediction_pipeline/boltz_worker.py` only.
- Tests: focused Boltz runtime handoff regressions, including a compact enrichment-boundary fixture.
- Public interfaces: no signature or return-schema change; the existing result fields receive their already-specified validated values.
- Data formats and migration: none.
- Legacy paths: unchanged.
- Explicit non-goals: retry/resume, promotion of the nine existing base bundles, readiness, budget governance (including the observed 40.31 versus 11.25 GPU-minute overrun), execution identity, Store schema, and scientific protocol.
