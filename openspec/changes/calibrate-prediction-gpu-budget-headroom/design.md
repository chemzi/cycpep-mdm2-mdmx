## Context

See `proposal.md` for motivation. The current public budget contract uses one integer `prediction_gpu_slot_minutes_per_candidate` value, includes that value in the bootstrap input digest, admits approvals against the resulting estimate, and measures actual usage once as Worker claim-to-closure elapsed wall time. Two complete n=2 observations now exist: 20.078 minutes and 22.338975 minutes. The latter correctly exceeded the 22-minute approval; process timings and the Orchestrator ledger show no duplicate charge and one GPU slot.

## Goals / Non-Goals

**Goals:**

- Calibrate the existing Prediction-only default against the current maximum production observation with explicit variability headroom.
- Preserve the existing public seams, plan identity binding, admission check, and actual completion enforcement.
- Require a fresh plan and approval while leaving the failed 22-minute invocation immutable.

**Non-Goals:**

- Changing the GPU-slot wall-minute accounting interval or measuring active-kernel time.
- Adding adaptive telemetry, persisted benchmark stores, new configuration fields, or a general estimator framework.
- Changing approval structure, completion enforcement, retry, artifact reuse, readiness, scientific protocol, transaction ownership, or Store schema.

## Decisions

### 1. Recalibrate the existing integer per-Candidate default to 15

The authoritative maximum n=2 observation is 22.338975 minutes, 11.3% above the earlier 20.078-minute observation. Use the existing coarse integer tunable at 15 minutes per Candidate, producing a 30-minute n=2 plan with about 34% operational headroom. This favors completing the small smoke over fitting a precise estimator to two samples.

Alternative: set 13 per Candidate (26 total). Rejected as unnecessarily tight for a coarse deployment budget after the prior under-estimation. Alternative: add fixed-plus-variable estimator fields. Rejected because two observations do not justify a new model and it would expand the public configuration surface.

### 2. Keep all governance wiring unchanged

Only the existing Prediction-specific default and its behavioral expectations change. The bootstrap input digest already binds this value, so the new default naturally produces a fresh immutable plan ID. The existing shared admission helper and completion-time actual usage check remain authoritative.

Alternative: raise the approval independently of the plan estimate. Rejected because that would detach approval from Planner-owned budget semantics. Alternative: add tolerance to completion. Rejected because it would weaken the fail-closed ceiling.

## Risks / Trade-offs

- [A later production run can still exceed 30 minutes] → Actual completion enforcement remains fail closed; a future recalibration must use new authoritative observations rather than silently widening tolerance.
- [Linear per-Candidate extrapolation is coarse] → Retain the existing narrow tunable until enough production observations justify a separately governed estimator model.
- [A higher estimate increases approved spend] → Approval admission exposes the full 30-minute ceiling before execution, and the smoke remains fixed at n=2.

## Migration Plan

Deploy the code-only default change with no schema migration. Do not alter or resume the failed 22-minute invocation. Build a fresh plan from the same approved n=2 project, approve exactly the new Planner-owned 30-minute estimate, and run a fresh Launcher smoke. Rollback is the code commit only; persisted plans and approvals remain immutable.
