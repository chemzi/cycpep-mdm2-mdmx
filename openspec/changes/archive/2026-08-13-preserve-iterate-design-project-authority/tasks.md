## 1. Regression Seams

- [x] 1.1 Add a red public Design CLI regression proving an explicit approved project config is loaded into `ProjectContext`, while omission preserves the legacy default constructor and an invalid explicit input fails before route execution.
- [x] 1.2 Add a red production-shaped `iterate_design` handler regression with `project_config=None`, a coordinate-bound project in Store-backed State, and a deliberately different ambient default; prove every job's argv and process environment point to one exact invocation snapshot.
- [x] 1.3 Add a Worker transaction regression where job A emits a CandidateUpdate and job B fails, proving no formal Candidate, `candidate_registered` Evidence, or successful Design result is published.

## 2. Narrow Authority Handoff

- [x] 2.1 Extend the Design CLI with additive `--project-config` loading through the existing public `ProjectContext`; keep the omitted-option path unchanged.
- [x] 2.2 After the existing project-digest gate, atomically write one non-authoritative attempt-local project snapshot and pass the same path to every `iterate_design` subprocess through both `--project-config` and `CYCPEP_PROJECT_CONFIG`.
- [x] 2.3 Confirm no Planner, budget, approval, retry, scientific protocol, Prediction, Critic, Launcher, Store schema, or task/result contract changes were introduced.

## 3. Verification and Delivery

- [x] 3.1 Run the focused CLI/Execution/transaction tests, including digest mismatch proving zero snapshot/process launches, then run the full suite.
- [x] 3.2 Run Architecture Gate, strict OpenSpec validation, Python compile checks, and `git diff --check`.
- [x] 3.3 Obtain independent high-reasoning Spec and Standards reviews; fix all P0/P1 findings and stop expanding scope when P0/P1=0.
- [x] 3.4 Archive the verified change locally, commit/push the feature branch, and use `gh` to create a ready PR without merging it during full-auto mode.
- [x] 3.5 Deploy the verified feature commit to an isolated remote checkout and start a fresh minimal `design.n=2` Launcher; keep all prior failed invocations immutable.
