## Why

The production `evaluate_new_design_candidates` task estimated two Candidates at 2.5 GPU minutes, while the real approved protocol occupied the single GPU task slot for 20.08 minutes. Because the ceiling was checked only during completion, the full scientific run finished and was then rolled back as `gpu_minutes_exceeded`.

## What Changes

- Give initial Prediction bootstrap tasks a benchmark-backed GPU-slot wall-time estimate for the exact Candidate count.
- Define existing `estimated_gpu_minutes` / `max_gpu_minutes` accounting for GPU tasks as allocated GPU-slot wall minutes from claim through closure.
- Reject an approval whose GPU-minute ceiling is below the selected tasks' benchmark-backed estimates, before any task can be claimed or scientific work can start.
- Preserve completion-time actual-usage enforcement as the final authority.
- Do not change Prediction readiness, scientific protocol, retry behavior, transaction ownership, Store schema, or old failed invocations.

## Capabilities

### New Capabilities

- `execution/prediction-gpu-budget-governance`: Benchmark-backed Prediction estimates and pre-execution approval admission for GPU-slot wall-minute ceilings.

### Modified Capabilities

None.

## Impact

- Planner configuration and initial Prediction bootstrap plan construction.
- Shared approval validation at the Planner/Orchestrator execution-budget boundary.
- Focused Planner, Orchestrator, and Worker contract regressions.
- No public function signature, Store schema, scientific artifact, readiness, retry, or migration change.
