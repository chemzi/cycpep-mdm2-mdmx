## Context

See `proposal.md` for motivation and `specs/workflow/exploration-decision-design-materialization/spec.md` for behavior. At the frozen base, `agents/planner/task_builder.py::_materialize_design_jobs` owns allocation, seed derivation, configured-length resolution, and an implicit ambient `experience` read/write fallback. E3-A provides a canonical dict already validated by `ExplorationDecision.from_dict()` at `state["_frozen_exploration_decision"]`.

The active Launcher is isolated from this worktree, and the change cannot touch Planner plan construction, schemas, contracts, Store/runtime state, execution, prediction, Design, Critic, project configuration, protocol, or thresholds.

## Goals / Non-Goals

**Goals:**

- Introduce a small pure Planner-local module that resolves effective lengths from approved per-target configuration plus the optional frozen decision.
- Keep `_materialize_design_jobs` as the compatible call seam and make only its length selection delegate to the pure module.
- Preserve the legacy no-job early return before Decision/length validation, while keeping target-scope and approved-envelope rejection explicit for requests that can materialize jobs.
- Remove all Planner job-materialization imports and calls to ambient experience/Evidence APIs.

**Non-Goals:**

- Revalidating or rebuilding ExplorationDecision, changing its schema, or changing the fixed E3-A State key.
- Weighted allocation, including 1:2:9; schema v1's canonical weight is only a deterministic length-set representation.
- Changing allocations, routes, proposal counts, seeds, plan/action schemas, protocols, thresholds, project configuration, workers, transactions, or scientific execution.
- Expanding into `workflow/service.py`, the Prediction executor, transaction ownership, or any active runtime data. If implementation requires any of those, stop and report the blocker instead of revising this change's scope.

## Decisions

### Use a pure length-policy module behind the existing job builder

`decision_materialization.py` will accept the frozen canonical dict, required target IDs, and the approved length sets already derived from project configuration. It returns effective per-target length sets or raises `PlannerContractError`. The existing job builder retains allocation and seed ownership. This isolates decision interpretation from job construction without changing the caller in `task_builders.py`.

Alternative considered: embed decision branching directly in `_materialize_design_jobs`. Rejected because it would keep validation, allocation, seed derivation, and policy interpretation in one function and expand the existing responsibility smell.

### Trust E3-A validation but fail closed at the E3-B seam

For materializable requests, the module will not call `ExplorationDecision.from_dict()` again. It trusts canonical status, weight, and preferred-length equivalence from E3-A, and enforces only materialization invariants that depend on current Planner inputs: exact required-target scope and containment within every target's approved envelope. This avoids importing the upstream scientific evaluator or accessing ambient Evidence while still rejecting unsafe materialization.

Alternative considered: reconstruct the full contract in Planner. Rejected because E3-A explicitly supplies an already-validated canonical dict and duplicate validation would couple Planner to upstream Evidence/scientific policy internals.

### Preserve target-specific approved lengths for no-adjustment

Each target's explicit `design.lengths`, or `[8, 10, 12]` when absent, remains its approved job envelope. `no_adjustment` returns those sets unchanged. `adjustment` replaces every scoped target's job lengths with the single canonical proposed set after containment checks. No allocation is derived from weights.

Alternative considered: apply the decision's intersection baseline even for `no_adjustment`. Rejected because `no_adjustment` explicitly preserves approved lengths and should not narrow target-specific configuration.

### Preserve the legacy no-job early-return boundary

`_materialize_design_jobs` returns `[]` immediately when `requested < 1` or `required_targets` is empty, before reading project target lengths or interpreting the frozen Decision. There is no job policy to materialize in those cases, and E3-A already owns formal Decision binding. For all other requests, length policy is resolved for the complete required-target scope before the allocation loop appends any job, so target/envelope failures remain atomic without creating a new exception on the legacy no-op path.

Alternative considered: validate the frozen Decision even when no jobs can be allocated. Rejected because it changes the established `_materialize_design_jobs` no-op contract and duplicates E3-A binding responsibility without protecting a materialized job.

## Risks / Trade-offs

- [The frozen dict guarantee is violated upstream] → E3-B rejects the materialization-specific malformed or mismatched fields it consumes; full provenance validation remains owned by E3-A.
- [A multi-target adjustment is valid for the decision intersection but configuration drifts] → containment is checked against every current target envelope and fails closed without mutating configuration.
- [Legacy tests encode ambient experience consumption] → replace that obsolete expectation with focused regressions that prove no ambient access and preserve the upstream experience module itself.

## Migration Plan

Target the shared `e3/closed-loop-runtime` branch only and do not follow later `integration/data-integrity-transaction` updates. Rebase E3-B onto the latest shared branch, rerun all gates and high-reasoning reviews, then merge PR #78 into that shared branch as explicitly authorized. Rollback is a commit revert: the existing `_materialize_design_jobs` signature and output shape remain unchanged, and there is no data migration or runtime deployment. Do not deploy or pull into the production checkout until the active Launcher reaches a terminal boundary or the user explicitly authorizes deployment.
