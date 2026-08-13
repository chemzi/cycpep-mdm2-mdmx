## Why

A real production `evaluate_new_design_candidates` run proved that one AF2 complex model can violate the existing cyclic-geometry precondition while sibling models and every required scientific runtime remain healthy. The Prediction enrichment owner currently misclassifies that model-level scientific rejection as `execution_process_failed`, rolling back the entire Candidate batch instead of publishing complete, explicit negative scientific evidence.

## What Changes

- Add a narrow typed Rosetta scientific-rejection artifact for the existing `rosetta_cyclic_bond_open` precondition.
- Require every declared complex prediction identity to appear in exactly one of a successful Rosetta output or typed Rosetta rejection.
- Preserve rejected-model PRODIGY output as diagnostic evidence, but compute canonical L3 `dg`, `sc`, and `dSASA` from the same Rosetta-eligible model cohort; never substitute a score, omit the model identity, or repair its coordinates.
- Make any accepted model-level rejection explicitly fail L3 and yield the existing terminal `needs_optimization` status with L3 in `failed_layers`, including when every model is rejected and no numeric aggregate exists.
- Keep deployment/runtime/version/timeout/process/malformed-output failures task-fatal and preserve invocation-level transactional publication.
- **BREAKING**: bump the Prediction artifact/protocol contract so old bundles cannot be resumed as if they contained model-level rejection evidence.

## Capabilities

### New Capabilities

- `evaluation/prediction-model-scientific-rejections`: Typed per-model scientific rejections, exact coverage, aggregation, terminal-negative status, and Worker transaction semantics.

### Modified Capabilities

None.

## Impact

- Affected owners: Prediction enrichment, artifact schema/loader, artifact inventory/provenance, metric collection/battery evaluation, transaction effects, versioned Prediction protocol, and focused Execution Worker regressions.
- Public data format changes: the versioned artifact bundle may carry `rosetta_rejections`; protocol/artifact compatibility identity changes accordingly.
- Public Python call signatures and Store schema do not change.
- No migration or automatic reuse of old/partial bundles; a new invocation under the bumped protocol must generate the new evidence.
- Non-goals: no Launcher, Planner, Critic, approval, budget, retry, readiness-table, scientific-threshold, coordinate-repair, or predictor-protocol change. Existing failed invocations remain immutable.
