## 1. Benchmark-backed Prediction planning

- [x] 1.1 Add public-contract regressions proving an n=2 initial Prediction bootstrap task carries a 22 GPU-slot wall-minute estimate, unrelated GPU estimates remain unchanged, and different dedicated estimator configuration produces a different immutable plan ID.
- [x] 1.2 Add the narrowly scoped Planner configuration and action-aware estimation implementation without changing Prediction protocol, readiness, or task scope.

## 2. Pre-execution budget admission

- [x] 2.1 Add Planner and Orchestrator regressions proving a 2.5-minute approval is rejected for the n=2 task, initialize creates no run, authorize leaves an existing run awaiting approval and unclaimable, and a covering ceiling passes.
- [x] 2.2 Implement one shared plan-contract admission helper and reuse it from Planner approval creation and Orchestrator approval ingestion; preserve completion-time actual usage enforcement.

## 3. Verification and delivery

- [x] 3.1 Run focused Planner/Orchestrator/Execution tests, the full suite, lint/type checks where configured, Architecture Gate, strict OpenSpec validation, and `git diff --check`.
- [x] 3.2 Complete independent Spec and Standards reviews, resolve all P0/P1 findings, archive/sync the OpenSpec change, and verify the archived strict contract.
- [ ] 3.3 Commit, push, create and merge the PR with `gh`, then deploy the merge commit and run a fresh fully automatic n=2 Launcher smoke without modifying the old failed invocation.
