## Why

Planner currently chooses peptide lengths from target configuration or an ambient experience/Evidence fallback, so an upstream frozen `ExplorationDecision` cannot deterministically govern the existing `iterate_design` jobs. E3-B must make that already-validated decision the explicit, closed input to job materialization without touching the active Launcher, execution, prediction, or scientific runtime.

## What Changes

- Add a pure Planner-local materialization boundary that derives each design job's lengths from approved target configuration and `state["_frozen_exploration_decision"]`.
- Preserve explicit configured lengths, the `[8, 10, 12]` fallback, proposal counts, routes, target allocation, seeds, protocol, thresholds, and project configuration unless a canonical `adjustment` narrows the approved length envelope.
- Preserve the legacy no-op return `[]` when `requested < 1` or no targets are required; only materializable requests fail closed when a frozen decision targets a different target or proposes lengths outside the approved envelope.
- Remove Planner job construction's implicit calls to `consume_experience_preference()` and `record_applied_preference()` and any ambient Evidence fallback.
- Keep the existing `iterate_design` action and `design_jobs` data shape; do not change E3-A's frozen-State interface or any execution/runtime ownership.

## Capabilities

### New Capabilities

- `workflow/exploration-decision-design-materialization`: Deterministic Planner-local conversion of a validated frozen ExplorationDecision into existing iterate_design job lengths.

### Modified Capabilities

- `workflow/planner-exploration-decision-input`: Supersede E3-A's phase-local non-operative and ambient-fallback behavior so the explicitly bound Decision governs only iterate-design lengths and Decision absence uses the deterministic approved/static length policy without ambient experience.

## Impact

- Affected code is limited to `agents/planner/task_builder.py`, a new `agents/planner/decision_materialization.py`, focused tests, and this OpenSpec change.
- The existing `_materialize_design_jobs` signature and `design_jobs` output format remain compatible; the only new consumed State field is the fixed E3-A interface `state["_frozen_exploration_decision"]`.
- No schema, Action, Store, transaction, runtime data, approved project configuration, protocol, threshold, Worker, Design executor, or migration changes are introduced.
- The legacy `experience.py` API remains available for the upstream ExplorationDecision builder, but Planner no longer consumes or records ambient experience.
