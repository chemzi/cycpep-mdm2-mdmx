## Context

See `proposal.md` for motivation and `specs/workflow/planner-exploration-decision-input/spec.md` for observable behavior.

The frozen baseline already contains the public immutable `contracts.exploration_decision.ExplorationDecision` and canonical `object_sha256` helper. `agents/planner/plan_builder.py` copies caller State before task construction, computes plan identity in `_plan_input_digest`, and emits Critic provenance in `source`. The Planner plan schema closes the source object with `additionalProperties: false`, so Decision provenance requires an additive schema change. Existing task construction falls back to ambient experience and records an applied preference when project target lengths are missing. That behavior remains unchanged for legacy calls but must be disabled for the explicit Decision path.

The implementation runs in an isolated worktree from `02c54edeb3580d58877e7c7bf18b79a7f75df162`. No Launcher lifecycle command, Worker, scientific executor, active runtime directory, or formal Store/Evidence instance participates in development or validation.

## Goals / Non-Goals

**Goals:**

- Add one explicit, optional, public Planner input without breaking existing callers.
- Reuse the E2 contract as the sole validator for Decision internals and add only cross-artifact handoff checks in Planner.
- Make plan provenance and identity deterministically bind the complete validated Decision.
- Keep all Decision data confined to the invocation-local State copy and returned plan.
- Prove legacy identity compatibility and non-application to task materialization.

**Non-Goals:**

- Interpreting `adjustment` or converting relative weights into jobs, counts, lengths, or seeds.
- Reading/writing Evidence, State persistence, project runtime data, approvals, plans, or transactions.
- Changing task materialization beyond the authorized no-ambient marker guard, or changing experience, E2 production/contracts, workflow, execution, Prediction, Design, Critic, Launcher, Orchestrator, or approved project configuration.

## Decisions

### D1. Restore the Decision once through the public E2 contract

`build_plan()` receives an optional mapping and immediately passes it to `ExplorationDecision.from_dict()`. Planner retains the resulting immutable object plus its canonical `to_dict()` serialization. Planner validation checks only relationships between that already-valid object and current Planner/Critic inputs: `project_id`, `workflow_id`, `source_round`, `applies_to_round == source_round + 1`, `prediction_run_id`, and `target_ids` against `required_targets`.

Alternative considered: reproduce selected E2 field checks in Planner. Rejected because it creates a second validator that can drift from the formal contract.

### D2. Validate after Planner identity resolution and before task construction

Planner copies caller State, removes any ambient `_frozen_exploration_decision` value as non-authoritative, then loads/validates the Critic report and resolves the authoritative project/workflow/source-round tuple using its current seam. It validates an explicitly supplied Decision handoff and injects the canonical dictionary into that local State copy under `_frozen_exploration_decision` before task builders run. The underscored key is therefore invocation-owned and cannot be supplied through State authority.

Target binding first verifies that Critic `required_targets` is a non-empty unique sequence of non-empty strings, then compares a sorted copy with the canonical Decision target tuple. It does not mutate the Critic sequence consumed by task builders and does not consult project configuration to invent or widen the target set.

Alternative considered: inject before Critic/workflow resolution or recover missing bindings from project config. Rejected because either order lets the Decision influence authority selection instead of being checked against it.

### D3. Bind Decision ID and canonical payload digest explicitly and conditionally

Calculate `decision_sha256 = object_sha256(validated_decision.to_dict())`. Extend the plan-digest semantic object with a Decision projection containing only `decision_id` and `decision_sha256`, but add that projection only when a Decision exists. This avoids relying on a broad hash of the private local State and guarantees the required Decision binding is reviewable.

Alternative considered: always add a `None` Decision field or depend on `_frozen_exploration_decision` being hashed indirectly. Rejected because the former drifts legacy plan IDs and the latter obscures the precise identity contract.

### D4. Add source provenance only on the explicit path

Assemble the legacy Critic source first. When a Decision is present, add `exploration_decision_id`, `exploration_decision_sha256`, and `exploration_decision_input_digest`. The schema permits these three properties as an all-or-none conditional set while retaining `additionalProperties: false`. No schema-version bump is needed because existing documents remain valid and readers already accept the same top-level version with additive optional provenance.

Alternative considered: emit nullable fields for every plan. Rejected because Decision absence must preserve exact legacy source shape and digest/ID.

### D5. Treat task equality as an acceptance invariant

The narrow task-builder change reads only whether `_frozen_exploration_decision` exists in the invocation-local State. When present, missing configured lengths skip both legacy ambient preference functions and proceed directly to the existing static `[8, 10, 12]` fallback; the Decision payload and adjustment are never read. When absent, legacy ambient behavior is unchanged. Focused tests compare the complete task/budget/approval/execution surfaces across different valid Decisions and exercise missing-length inputs with ambient functions configured to fail if called. This makes “recorded but not applied” observable rather than conventional.

Alternative considered: populate missing lengths from the Decision's approved envelope. Rejected because even baseline Decision data influencing jobs would apply the Decision before E3-B. A private marker guard preserves the task builder's existing static fallback and changes no configured project data or digest.

## Risks / Trade-offs

- **[Optional source fields may be partially emitted]** → build them from one validated binding object and enforce an all-or-none schema condition.
- **[Target ordering could create a false mismatch or alter legacy tasks]** → validate Critic scope, sort only a comparison copy, and preserve the original sequence for task builders; do not collapse duplicates or accept ambiguous scope.
- **[Local State injection could accidentally affect task construction later]** → use the private key only as a boolean no-ambient guard, never inspect its contents in task construction, and lock task equality across Decisions with focused tests.
- **[Legacy task construction can consult ambient experience]** → permit the single `task_builder.py` guard authorized after review and preserve the absent path.
- **[Caller State can spoof the private marker]** → remove the reserved key immediately after copying State and inject it only after explicit Decision validation.
- **[Legacy plan IDs could drift]** → conditionally omit all Decision semantics when absent and compare against a frozen-baseline fixture/result.
- **[Contract exceptions expose a different error type]** → preserve the public E2 contract error for invalid Decision payloads and use Planner contract errors only for cross-artifact handoff mismatches; tests document both boundaries.

## Migration Plan

1. Ship the optional Planner input, additive schema allowance, and focused isolated tests with no caller wired by workflow/Launcher.
2. Existing calls that omit the input remain unchanged; no runtime data migration or deployment is required.
3. Before merge, synchronize only the latest `e3/closed-loop-runtime` HEAD into the feature branch and rerun the relevant verification gates. Do not rebase or otherwise follow moving `integration/data-integrity-transaction` during E3-A development.
4. Open the Draft PR against `e3/closed-loop-runtime`; do not merge or deploy it. The shared E3 branch is synchronized with the latest integration branch only after E3-A and E3-B have both landed there.
5. Rollback removes the optional input path and schema properties. Decision-bound plans created only in isolated tests remain non-production artifacts.
6. A separately approved E3-B change may later consume the frozen local Decision to materialize design work; this change provides no such behavior.
