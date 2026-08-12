## 1. Lock the observed failures as tests

- [x] 1.1 Characterize Route A's current RFdiffusion, LigandMPNN, and refold failure fallbacks and prove the legacy `design_rfpeptides()` list-return contract remains unchanged.
- [x] 1.2 Extend `test_design_initial.py` with normal-zero-result, classified-tool-failure, unclassified-exception, empty legacy completion, conflicting terminal receipt, failure-write failure, and no-retry cases.
- [x] 1.3 Characterize the Prediction owner's battery-to-status and handoff Critic-readiness semantics before extracting them.
- [x] 1.4 Extend `test_prediction_launcher_contract.py` with owner-ready, missing-evidence, all-pending, mixed, non-ready-terminal, candidate-set mismatch, record-integrity, and battery/status contradiction fixtures.
- [x] 1.5 Extend `test_workflow_service.py` with launch/status/resume blocker parity and assertions that blocked Design never calls Prediction and non-ready Prediction never calls Critic.

## 2. Make initial Design terminal state truthful

- [x] 2.1 Add an opt-in Launcher initial Route A adapter that propagates classified RFdiffusion, LigandMPNN, and refold tool failures as a typed Design outcome while preserving legacy route behavior.
- [x] 2.2 Add the correlated `design_initial_failure` receipt with distinct `initial_design_no_valid_candidates` and `initial_design_scientific_tool_failed` blockers.
- [x] 2.3 Make initial Design persist and validate exactly one appropriate failure receipt for a normal empty result or classified tool failure, then raise without writing completion.
- [x] 2.4 Tighten recovery validation so completion requires a non-empty existing candidate set and any malformed, duplicate, or completion/failure conflict returns `design_recovery_ambiguous` without retry.
- [x] 2.5 Run `python -m unittest test_design.py test_design_initial.py` and fix only regressions caused by this change.

## 3. Classify Launcher Prediction readiness at its owner boundary

- [x] 3.1 Extract the existing battery-to-status function and Critic-readiness set into one public Prediction-owned contract used by pipeline handoff generation and Launcher-correlated validation.
- [x] 3.2 Extend correlated invocation validation to prove authoritative record digest, candidate/run/status binding, battery/status consistency, and exact input candidate-set coverage after existing envelope checks pass.
- [x] 3.3 Return `prediction_execution_incomplete` for records that the owner contract classifies as missing-evidence, pending, or otherwise non-ready; preserve existing integrity/correlation blockers for contradictory evidence.
- [x] 3.4 Adapt the Prediction formal boundary mapping to expose the new blocked result without adding scientific parsing to `workflow/service.py`.
- [x] 3.5 Run `python -m unittest test_prediction_launcher_contract.py test_prediction_pipeline.py` and fix only regressions caused by this change.

## 4. Integrate Launcher projection and documentation

- [x] 4.1 Confirm Design and Prediction boundary adapters return the owning blocked `FormalBoundary` so existing service coordination gives launch/status/resume the same code and boundary.
- [x] 4.2 Add the minimal service adjustment only if owner-boundary projection tests demonstrate a remaining mismatch; do not replay diagnostic errors as authority or copy scientific status policy into service.
- [x] 4.3 Update `docs/workflow_launcher.md` with the Design zero-result/tool-failure distinction, Prediction owner-readiness gate, blocker codes, legacy fail-closed behavior, and the explicit `critic-project-binding` handoff.
- [x] 4.4 Run `python -m unittest test_workflow_service.py test_workflow_boundaries.py test_design.py test_design_initial.py test_prediction_launcher_contract.py test_prediction_pipeline.py`.

## 5. Verification and review

- [x] 5.1 Run `openspec validate enforce-launcher-boundary-truth --strict` and `openspec verify enforce-launcher-boundary-truth` if the installed CLI exposes that command.
  - Result: strict validation passed; the installed OpenSpec CLI does not expose `verify` (`unknown command 'verify'`).
- [x] 5.2 Run the full repository unittest suite, `python scripts/architecture_gate.py`, and all configured lint/type checks documented by the repository; record exact commands and results in the OpenSpec task state.
  - Result: `python -m unittest discover -b` passed 680 tests with 4 skips; `python scripts/architecture_gate.py --baseline architecture_baseline.json` passed with zero new violations; `npm run lint` and `npm run typecheck` passed with `NODE_OPTIONS=--max-old-space-size=4096`; the configured Python compile smoke passed.
- [x] 5.3 Review the final diff against `ENGINEERING_STANDARD.md`, this change's proposal/spec/design, compatibility decisions, the no-shadow-state/no-fabricated-evidence constraints, and the prohibition on Critic project-binding edits; resolve all actionable findings before completion.
  - Result: final scoped diff and `git diff --check` passed; this change modifies no Critic project-binding implementation file, and `workflow/service.py` required no change.
- [x] 5.4 Re-run the real approved-project Launcher lifecycle on the target machine and capture formal evidence that it either reaches the next legitimate boundary or stops with the owning stable blocker across both `launch` and `status`.
  - Result: target-machine run `launcher_c9235ba8f0f347f196ba95aa2500be08` classified the missing RFdiffusion runtime as `initial_design_scientific_tool_failed` at Design; launch, status, resume, and subsequent status preserved that owner blocker and did not enter Prediction. Preserved run `launcher_03a8ecab979a43b6bc56055e16fc9723` was re-read as `prediction_execution_incomplete` at Prediction across status/resume/status and did not re-enter Critic.

## 6. Close Design review P1s

- [x] 6.1 Add strict postcondition regressions for RFdiffusion exit-zero missing/partial backbone output, malformed backbone or missing binder chain, unavailable LigandMPNN model/config, exit-zero missing/malformed generated sequence output, and refold preparation/output failures while preserving all legacy fallbacks.
  - Result: strict adapter regressions cover every required RFdiffusion, LigandMPNN, and refold preparation/output postcondition; paired legacy assertions preserve the historical behavior.
- [x] 6.2 Make the Launcher strict route raise the existing typed scientific-tool/output failure for every failed required postcondition; allow `initial_design_no_valid_candidates` only after required generation succeeds and later scientific filtering/quality/closure eliminates all candidates.
  - Result: strict failures propagate as `ScientificToolExecutionError`; a generated sequence set eliminated by the existing scientific filter remains the distinct normal zero-result path.
- [x] 6.3 Route Launcher Initial Design candidate publication into an invocation-owned collection of existing `CandidateUpdate` effects and atomically commit candidates, authoritative candidate-registration Evidence, and completion through the existing Store transaction seam.
  - Result: the strict adapter stages CandidateUpdates and `SQLiteStore.commit_transaction` publishes the candidates, authoritative registrations, and completion together.
- [x] 6.4 Add the partial-publication regression: stage candidate A, fail candidate B or a later job, prove the tool-failure blocker, no formal A candidate/event, no dedup visibility, and no automatic retry.
  - Result: the regression drives two sequences through the real Route A registration path, proves candidate A reaches `_publish_candidate`, then proves all seven required observations after candidate B refold fails.
- [x] 6.5 Prove the successful path publishes all staged candidates and completion in one formal transaction, while legacy `design_rfpeptides()` behavior and non-authoritative intermediate files remain unchanged.
  - Result: success asserts one shared committed transaction; the unchanged legacy entry point still calls the core route without strict or staging arguments.

## 7. Re-verify PR 68

- [x] 7.1 Run Design focused tests, the full unittest suite, Architecture Gate, strict OpenSpec validation, configured lint/type checks, and `git diff --check`.
  - Result: focused 92 tests and full 689 tests passed with 4 skips; Architecture Gate reported no new violations; strict OpenSpec, compile, web lint/typecheck, and diff check passed.
- [x] 7.2 Synchronize the PR head with the latest `integration/data-integrity-transaction`, rerun affected gates if the base changed, and review the final PR diff for P0/P1 findings without expanding scope.
  - Result: merged integration head `dc28e8e`, reran the full and affected gates, and completed independent Standards and Spec reviews with P0=0/P1=0 after closing their Design-only findings.

## 8. Close final Design review P1s

- [x] 8.1 Add a regression proving a non-empty completion without `transaction_id` is `design_recovery_ambiguous`, then require every successful completion to name a committed transaction whose same-transaction authoritative candidate IDs exactly equal the completion IDs.
  - Result: the reader now rejects a missing/blank transaction binding, non-committed transaction, and any malformed or non-exact same-transaction candidate-registration set.
- [x] 8.2 Add a regression with a complete stale RFdiffusion backbone set plus current exit-zero/no-write, then clean the strict output prefix before subprocess launch so the invocation reports `initial_design_scientific_tool_failed`.
  - Result: strict RFdiffusion removes only its output-prefix PDBs before subprocess launch; the current exit-zero/no-write regression reaches the required-output failure.
- [x] 8.3 Run focused tests, the full unittest suite, Architecture Gate, strict OpenSpec validation, scoped diff check, and final P0/P1 review; stop without expanding scope when P0/P1=0.
  - Result: Initial Design 27 tests and legacy Design 18 groups passed; final full unittest discovery passed 693 tests with 4 skips; Architecture Gate reported zero new violations; strict OpenSpec and scoped diff check passed; independent Standards and Spec reviews both concluded P0=0/P1=0.
