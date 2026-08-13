## Why

The first benchmark-backed Prediction estimate still admitted a production n=2 task at 22 GPU-slot wall minutes, but a fresh complete run measured 22.338975 minutes and was correctly rolled back at completion. The estimator therefore needs a narrow recalibration that covers all observed production runs plus explicit runtime headroom, without weakening actual-usage enforcement.

## What Changes

- Recalibrate the existing `evaluate_new_design_candidates` per-Candidate estimate so an n=2 plan requests a conservative 30 GPU-slot wall minutes, leaving operational headroom above the highest observed complete production run.
- Keep the calibrated value in the existing immutable bootstrap plan identity so a fresh plan and approval are required.
- Preserve approval admission and completion-time measured-usage enforcement unchanged.
- Preserve the old failed invocation, including its 22-minute approval and failed transaction, unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `execution/prediction-gpu-budget-governance`: Replace the single-observation 22-minute n=2 calibration with a conservative two-observation 30-minute calibration.

## Impact

- Affected code: the existing Prediction-specific Planner configuration default and its focused bootstrap/approval regressions.
- Public interfaces: no signature or field changes; an existing configuration default changes from 11 to 15 GPU-slot wall minutes per Candidate.
- Data formats and Store schema: unchanged; no migration.
- Scientific protocol, Prediction readiness, execution identity, retry, transaction ownership, and actual GPU accounting: unchanged.
- Legacy behavior: existing persisted plans, approvals, failures, and transactions remain immutable and are not resumed or rewritten.
