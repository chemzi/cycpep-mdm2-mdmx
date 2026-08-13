## 1. Prediction publication identity

- [x] 1.1 Add a real transaction-writer regression that reproduces committed `prediction_recorded` Evidence without `prediction_run_id` and proves the production Launcher publication validator rejects the resulting exact-scope publication.
- [x] 1.2 Bind normal and typed-invalid `prediction_recorded` events to the adapter-owned Prediction run identity through the existing `record_event()` normalization seam; do not change Worker trace ownership or Launcher validation.
- [x] 1.3 Prove the committed record, battery, and handoff events share the same Prediction run identity, retain exact execution trace fields, and pass the real publication validator; retain missing/mismatched identity fail-closed regressions.

## 2. Verification and delivery

- [x] 2.1 Run focused transaction/publication tests, the full unittest suite, Architecture Gate, strict OpenSpec validation, and `git diff --check`.
- [x] 2.2 Complete independent high-reasoning Spec and Standards reviews and resolve every P0/P1 without expanding scope.
- [x] 2.3 Archive/sync the verified change, commit/push/create and merge PRs with `gh`, deploy the newest merged integration commit, and start a fresh fully automatic n=2 Launcher run while preserving the blocked invocation unchanged.
