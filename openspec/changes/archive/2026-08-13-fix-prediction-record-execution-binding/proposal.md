## Why

A fresh production n=2 Launcher run completed Prediction scientific execution, ingest, and transaction commit, but Launcher publication validation correctly blocked because transaction-produced `prediction_recorded` Evidence omitted the Prediction domain `prediction_run_id` carried by the same transaction's battery and handoff events. The formal writer and the existing exact publication proof must agree so committed Prediction work can cross the Launcher boundary without weakening correlation checks.

## What Changes

- Bind every transaction-managed `prediction_recorded` Evidence event to the existing Prediction domain run identity.
- Add a regression through the real transactional Prediction writer and Worker formalization seam, followed by the real Launcher publication validator.
- Preserve exact workflow/run/plan/task/attempt/transaction/candidate binding and all existing fail-closed tamper checks.
- Preserve the failed production invocation unchanged; validate the repair only in a fresh Launcher run.

## Capabilities

### New Capabilities

- `workflow/prediction-publication-binding`: Defines the exact formal identity contract shared by committed Prediction record, battery, and handoff Evidence at the Launcher publication boundary.

### Modified Capabilities

None.

## Impact

- Affected code: the existing Prediction transaction-effect writer and focused transactional/publication regressions.
- Public interfaces: unchanged.
- Data formats and Store schema: no new fields or migration; an existing `prediction_run_id` field is populated consistently on `prediction_recorded` events.
- Scientific protocol, Prediction readiness, Launcher validation, Planner, Critic, retry, transaction ownership, and execution identity: unchanged.
- Legacy behavior: historical Evidence and the blocked invocation remain immutable; no compatibility reader or repair migration is added.
