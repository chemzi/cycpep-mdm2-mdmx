## 1. Characterize and Specify the Materialization Seam

- [x] 1.1 Add focused failing regressions for explicit/fallback lengths, no-adjustment, narrowing adjustment, target/envelope rejection, preserved counts/routes/seeds, deterministic jobs, no ambient experience/Evidence access, and untouched project/protocol/threshold inputs.
- [x] 1.2 Replace the obsolete legacy regression that requires Planner to consume and record ambient experience, while retaining upstream experience coverage outside Planner materialization.
- [x] 1.3 Run every new and related test only with unittest fixtures and isolated temporary paths/Store; do not read, write, or migrate the active Launcher's Store/SQLite, Evidence, runtime locator, artifact root, project runtime directory, approval, plan, or transaction data.

## 2. Implement Pure Decision Materialization

- [x] 2.1 Add `agents/planner/decision_materialization.py` to resolve approved target length envelopes and optional canonical frozen-decision narrowing without I/O or weighting allocation.
- [x] 2.2 Redirect `_materialize_design_jobs` length selection through the pure module and remove Planner calls/imports for `consume_experience_preference()` and `record_applied_preference()`.
- [x] 2.3 Run focused Planner/materialization tests and correct only in-scope failures.

## 3. Verification and Review

- [x] 3.1 Run full unittest, Architecture Gate, strict OpenSpec validation, compile checks, and `git diff --check` in the isolated worktree; run no `workflow launch/resume`, ExecutionWorker drain, `evaluate_new_design_candidates`, `iterate_design`, or real scientific subprocess.
- [x] 3.2 Run `$openspec-verify-change` for implementation completeness, correctness, and design coherence; resolve all critical issues.
- [x] 3.3 Run independent high-reasoning Spec and Standards reviews against frozen base `02c54edeb3580d58877e7c7bf18b79a7f75df162`; resolve all P0/P1 findings and rerun affected gates.
- [x] 3.4 Confirm the changed-file set excludes every forbidden production/runtime path and that no Launcher, Worker, iteration, or scientific subprocess command was run. If implementation requires `workflow/service.py`, the Prediction executor, transaction ownership, or active runtime data, stop and report instead of expanding scope.
- [x] 3.5 Confirm `e3/closed-loop-runtime` exists at or after the frozen base; if E3-A landed first, rebase only onto that shared branch and rerun affected gates.
- [x] 3.6 Commit and push the isolated branch, create a Draft PR targeting `e3/closed-loop-runtime` with base/head/test evidence, and do not merge, deploy, or modify the production checkout.
