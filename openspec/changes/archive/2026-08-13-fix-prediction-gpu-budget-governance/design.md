## Context

See `proposal.md` for motivation and the capability spec for observable behavior. Current Planner metadata applies one generic proposal/candidate prior to every GPU action. For the initial Prediction bootstrap this produced 2.5 minutes for two Candidates, while production execution occupied the task's single GPU slot for 20.078 minutes. `execution.worker` already measures actual GPU usage as claim-to-closure elapsed time, and Orchestrator enforces the ceiling only at completion.

## Goals / Non-Goals

**Goals:**

- Make the initial Prediction estimate action-aware and calibrated to the real production n=2 benchmark.
- Use one public plan-contract helper for approval-time estimated-minute admission from both Planner and Orchestrator owners.
- Keep completion-time measured usage as the final enforcement boundary.

**Non-Goals:**

- Measuring GPU utilization or changing the meaning to active-kernel minutes.
- Mid-task cancellation, reservation accounting, retry, artifact reuse, readiness, scientific protocol, or transaction redesign.
- Modifying or resuming the old failed invocation.

## Decisions

### 1. Account in GPU-slot wall minutes

The existing Worker measurement (`elapsed_seconds / 60`) is retained and named explicitly in the behavioral contract. A task holding the single GPU scheduling slot consumes that slot even during CPU-bound preparation or serial gaps. This makes planning and completion compare the same unit.

Alternative: derive active GPU time from polling. Rejected because sampling is incomplete, hardware-specific, and would change an established governance contract beyond this repair.

### 2. Add an action-specific Prediction benchmark tunable

`PlannerConfig` receives an integer Prediction GPU-slot-minutes-per-Candidate prior. The default is 11, conservatively rounded from the real n=2 production observation so two Candidates are estimated at 22 minutes rather than below 20.08. `_compute_plan_metadata` uses it only for `evaluate_new_design_candidates`; all other actions retain their existing estimator. The dedicated estimator value is included in the initial bootstrap plan input digest so different budget semantics cannot reuse one immutable `plan_id`.

Alternative: raise the generic candidate factor. Rejected because it would silently change Design and unrelated GPU actions.

### 3. Admission belongs to the shared plan approval contract

A public helper in `contracts.plan` computes the selected GPU tasks' required estimated minutes and fails closed when an estimate is missing/non-numeric or the ceiling is insufficient. Planner approval creation and Orchestrator approval ingestion both call it. This avoids copying policy tables into Launcher, service, or Worker and ensures a manually constructed approval cannot bypass the owner boundary.

Alternative: check only in Launcher or Worker. Rejected because Launcher is not the budget authority, while Worker-time checking occurs after approval and potentially after claim.

### 4. Preserve completion enforcement

Admission compares approved ceiling to planned estimates; it does not promise runtime duration. Actual usage can vary, so existing completion enforcement remains unchanged and authoritative.

## Risks / Trade-offs

- [A single n=2 observation extrapolated per Candidate is coarse] → Keep the value configurable and conservative; future benchmark modeling is a separate change.
- [Runtime can still exceed estimate] → Completion-time ceiling remains fail-closed.
- [Existing plans with benchmark-required estimates cannot satisfy strict admission] → Admission applies when selected GPU tasks are formally approved under the current Planner contract; legacy schema behavior is not broadened or migrated in this change.

## Migration Plan

Deploy as a Planner/contract behavior update with no persisted-schema migration. Rollback is the code commit only; old plans and failed invocations are not rewritten. After merge, create a fresh n=2 plan and approval whose ceiling covers the new estimate, then run a new smoke invocation.
