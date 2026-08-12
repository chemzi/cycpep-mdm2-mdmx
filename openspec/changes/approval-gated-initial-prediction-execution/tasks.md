## 1. Characterize Current Authority Boundaries

- [x] 1.1 Add characterization tests proving the current Launcher calls direct ingest-only Prediction after committed Initial Design, while the registered `evaluate_new_design_candidates` handler is the only path that generates or reuses full scientific artifacts.
- [x] 1.2 Add characterization tests for Planner `plan()` reporting Prediction approval as the bootstrap next step, canonical Critic-plan source validation, explicit `candidate_scope.candidate_ids`, candidate-limit rejection, Worker transaction output inventory, and direct Prediction owner readiness.
- [x] 1.3 Add a preserved-run regression fixture proving a Launcher run with existing direct `prediction_invocation_started`, coherent pending handoff, and `prediction_execution_incomplete` remains on the legacy direct recovery path and receives no bootstrap records or mutations.

## 2. Define Path-Independent Prediction Execution Identity

- [x] 2.1 Add a public Prediction-owned execution identity contract that composes the existing Prediction protocol binding, ColabDesign commit, AF2 model/configuration identities, Boltz version/model/checkpoint content identity, PyRosetta version, PRODIGY version, and canonical configuration digest without absolute paths.
- [x] 2.2 Add tests proving identical scientific configuration at different executable/cache/repository/output paths has equal identity; tool, model, checkpoint, commit, protocol, or scientific configuration changes have different identity; and unknown/mismatched runtime identity fails closed.
- [x] 2.3 Extend the existing Prediction task parameter and execution receipt contracts to carry expected and observed execution identity while retaining runtime paths only in internal execution locator/process metadata and excluding them from plan/scientific identity and browser-safe output.
- [x] 2.4 Reuse existing tool/version/checkpoint validators in Worker preflight and receipts; do not add a parallel version probe or scientific executor, and characterize the existing `evaluate_new_design_candidates` scientific command sequence as unchanged.
- [x] 2.5 Update every newly generated `evaluate_new_design_candidates` task, including Critic-driven plans, to carry the same execution identity; add read-compatible but execution-fail-closed coverage for historical unstarted tasks lacking it and deterministic replanning from their immutable source.

## 3. Add Planner-Owned Bootstrap Prediction Plan

- [x] 3.1 Add a cohesive Planner bootstrap module/public entry point that validates Research completion plus Initial Design completion, committed transaction, and authoritative candidate registrations, then derives the exact committed candidate set without consulting projections or ambient artifact directories.
- [x] 3.2 Extend the canonical Planner plan schema and shared plan validator as a tagged union with `source.kind=initial_prediction_bootstrap`, binding approved project content, Launcher and Design identities, Design completion/transaction, exact candidate set, and Prediction execution identity; leave the existing Critic-report source variant unchanged.
- [x] 3.3 Build exactly one `evaluate_new_design_candidates` task with explicit full candidate scope, active protocol/execution identity, `reuse_or_generate_full`, candidate limit equal to the exact set size, and existing execution-budget approval contract; return a typed blocker instead of truncating an over-limit set.
- [x] 3.4 Persist, register, inspect, and idempotently recover the bootstrap plan through Planner's existing canonical plan file plus a tagged `planner_plan` Evidence source union, with shared payload validation, deterministic identity, no breaking `EvidenceLogger.planner_plan()` signature change, and no Critic-derived recommendation, synthetic report, or optionalized `critic_report_path` API.
- [x] 3.5 Add negative tests for mismatched Design completion/transaction registrations, changed project approval, duplicate or conflicting bootstrap plans, Critic fields in a bootstrap source, non-Prediction/additional tasks, scope reduction, and bootstrap plans misread as Critic-driven plans.
- [x] 3.6 Add approval tests proving no approval yields `awaiting_approval`, wrong plan/task/candidate/protocol/budget scope fails closed, and valid explicit approval permits the existing Orchestrator initialization without automatic approval or widening.

## 4. Bind Existing Worker Prediction Output to Owner Readiness

- [x] 4.1 Extend the formal Prediction execution result/receipt adapter only as needed to expose `prediction_run_id`, expected/observed execution identity, handoff output, committed transaction, Artifact IDs, and Evidence bindings from the existing `evaluate_new_design_candidates` action; do not change its scientific generation logic.
- [x] 4.2 Add a Prediction-owned execution validator that accepts the approved bootstrap plan/run/task/attempt/transaction binding, validates exact candidate and protocol/configuration scope, task output and formal Artifact/Evidence correlation, and reuses the existing public record/readiness contract rather than duplicating L1-L7 or status tables.
- [x] 4.3 Prove formal envelope `run_id` remains the Orchestrator run identity and payload `prediction_run_id` remains the domain identity, including different-value success and generic TRACE_KEYS conflict-before-commit regression coverage.
- [x] 4.4 Add Worker preflight-failure tests proving a missing or incompatible required tool/runtime marks the task failed, does not commit the Prediction transaction, publishes no authoritative handoff completion or Candidate/State/Artifact mutation, and retains bounded diagnostic process/locator evidence only.
- [x] 4.5 Add successful execution-contract tests proving complete/reused artifacts, records, handoff, formal Artifacts/Evidence, candidate patches, State effects, task output, and transaction are bound to the same approved task attempt and exact candidate set.
- [x] 4.6 Add owner-readiness tests proving all existing Critic-ready statuses permit completion, any one `prediction_pending` candidate returns `prediction_execution_incomplete`, and correlation/integrity contradictions retain their specific blocker instead of degrading to pending.

## 5. Coordinate the Bootstrap Lifecycle in Launcher

- [x] 5.1 Replace the new-run post-Design direct ingest call with bootstrap-plan inspection/creation, while preserving the public direct Prediction API for non-Launcher callers and using existing direct receipts as the forward-only legacy discriminator.
- [x] 5.2 Add focused pre-Critic coordination that projects bootstrap plan creation, `awaiting_approval`, approval validation, Orchestrator initialization/status, transaction recovery, Worker drain, task failure, formal Prediction recovery, owner readiness, and subsequent Critic invocation without making diagnostics authoritative.
- [x] 5.3 Ensure completion of the bootstrap Orchestrator run returns control to Prediction owner validation and Critic, then uses the unchanged Critic-driven Planner flow; do not treat bootstrap run completion as overall Launcher completion.
- [x] 5.4 Add the required internal diagnostic mirrors for bootstrap plan/run/task/Prediction identities without storing secrets or making deployment paths, diagnostics, process logs, State phase, CSV/JSON projections, or ambient artifact directories into recovery authority.
- [x] 5.5 Add launch regression proving committed Design with no current Prediction creates one bootstrap plan and returns `awaiting_approval`, runs no scientific tool, and writes no new direct ingest-only `prediction_invocation_started` event.
- [x] 5.6 Add approved-flow regression proving exact-scope Worker execution, committed formal Prediction, owner readiness, and only then Critic invocation and normal Critic-driven planning.
- [x] 5.7 Add crash/resume tests for plan persistence, approval attachment, Orchestrator initialization, active/unknown task attempt, unresolved transaction, Worker completion before diagnostic update, Prediction completion before Critic, and Critic completion before regular planning; prove completed expensive work is never repeated and unknown science is never auto-retried.
- [x] 5.8 Add command-parity tests proving launch/status/resume agree for existing plan awaiting approval, approved ready/running execution, terminal task failure, `prediction_execution_incomplete`, integrity/recovery blockers, and completed bootstrap transition; status remains read-only.
- [x] 5.9 Add mixed-history tests proving existing direct Prediction evidence prevents bootstrap creation, legacy failed runs are immutable, and contradictory direct/bootstrap authority fails closed without downstream Critic inspection.
- [x] 5.10 Add operator-explicit retry planning for a terminal failed bootstrap execution using the same Initial Design completion, committed Design transaction, project binding, and exact candidate set; publish a new immutable retry plan bound to the prior failure and return `awaiting_approval` without rerunning Research/Design or starting science.
- [x] 5.11 Add retry regressions proving ordinary launch/status/resume never retries, prior approval cannot authorize the retry plan, valid new approval uses the existing Orchestrator/Worker handler, repeated explicit requests are idempotent, and active/claimed/partial/unresolved/non-failed/completed executions cannot create a retry.
- [x] 5.12 Add multi-retry recovery coverage proving each failed plan/run/task/transaction stays immutable, every new execution has a new approved plan and Orchestrator identity, exact candidate scope never changes, and a later successful retry advances through owner readiness without repeating Research or Initial Design.

## 6. Documentation and Interface Accounting

- [x] 6.1 Update Launcher, Planner, Execution, and Prediction documentation with the new formal sequence, bootstrap-versus-Critic plan distinction, approval pause, exact candidate scope, formal recovery authority, path-independent scientific identity, and internal-only execution locators.
- [x] 6.2 Document the additive Planner bootstrap public interface and plan source variant, caller search, compatibility behavior, deployment preflight requirements, rollout/rollback boundary, and preserved direct/non-Launcher Prediction path per Engineering Standard API Stability.
- [x] 6.3 Document the explicit terminal-failure retry command/flag, new-plan/new-approval contract, unchanged Design authority, idempotency, and the prohibition on ordinary automatic retry or reopening failed task attempts.
- [x] 6.4 Confirm no production or documentation change introduces a generic environment/scientific identity framework, second tool-probing system, second Prediction materializer, Critic/L1-L7/threshold/Store schema change, candidate-scope reduction, automatic approval/retry, or preserved failed Launcher mutation.

## 7. Verification and Strict Review

- [x] 7.1 Run focused Planner bootstrap, plan/approval validation, Prediction identity, Worker/transaction, Artifact/Evidence, owner readiness, Launcher boundary, command parity, and recovery tests, including all negative regressions required by the spec.
- [x] 7.2 Run the relevant existing Planner, Prediction, Launcher, Critic-correlation, Orchestrator, Worker, transaction, recovery, Store, and protocol suites and record exact commands/results in this change.
- [x] 7.3 Run the full Python test suite, applicable lint/type/compile checks, `python scripts/architecture_gate.py`, strict OpenSpec validation, and `git diff --check`.
- [x] 7.4 Perform independent Spec and Standards reviews against a fixed baseline; resolve every P0/P1 finding, confirm the existing Worker handler is the sole scientific executor, and record final review results before requesting implementation completion.
- [x] 7.5 Run `$openspec-verify-change` after implementation and reconcile every completed checkbox with concrete code/test evidence; stop this change when all gates pass without taking unrelated P2 work.

## Verification Evidence

- Focused and relevant regression suites after rebasing onto the PR68/PR69 integration base: `python -m unittest -v test_planner.py test_planner_bootstrap_prediction.py test_protocol.py test_prediction_execution_identity.py test_prediction_launcher_contract.py test_prediction_transactional.py test_execution.py test_transactional_handlers.py test_orchestrator.py test_recovery_hardening.py test_workflow_boundaries.py test_workflow_bootstrap_prediction.py test_workflow_service.py test_workflow_cli.py test_workflow_runtime_locator.py test_workflow_critic_correlation_characterization.py test_contract_migration.py` — **277 passed, 3 skipped**.
- Full Python suite after rebasing onto the PR68/PR69 integration base: `python -m unittest discover` — **718 passed, 4 skipped**. One initial Windows `os.replace` file-lock error passed both isolated rerun and the complete clean rerun.
- Compilation: `python -m compileall -q agents contracts execution prediction_pipeline workflow` — **passed**.
- Architecture: `python scripts/architecture_gate.py` — **passed, 0 new violations on every axis**.
- OpenSpec: `openspec validate approval-gated-initial-prediction-execution --strict` — **passed**.
- Whitespace: `git diff --check` — **passed** (Git emitted only platform line-ending notices).

## Final Review

- **Spec — PASS.** Review found and closed the bootstrap-to-Critic correlation bug, direct/bootstrap dual-authority ambiguity, incomplete plan/run/receipt/Artifact correlation, and incomplete retry-chain recovery proof. The resulting flow uses the existing registered `evaluate_new_design_candidates` handler as the sole scientific executor and preserves direct Prediction only for legacy/non-Launcher callers.
- **Standards — PASS.** Public interface changes are additive and documented; transaction ownership, action closed-world, and ProjectContext boundaries remain intact. Bootstrap coordination was extracted from `workflow/service.py`, and Prediction completion validation was decomposed so Architecture Gate reports no new file/function complexity violation. No P0/P1 finding remains.
- **Scope — PASS.** No new materializer/prober, no L1-L7/Critic/threshold/Store-schema/scientific behavior change, no candidate reduction, no automatic approval/retry, and no historical run mutation were introduced.

## 8. Close PR73 P1 Contract Gaps

- [x] 8.1 Add failing regressions proving observed execution identity is runtime-derived rather than copied, missing observed identity is rejected by the transaction adapter, installed PRODIGY mismatch fails validation, a `venv/bin/python` symlink retains virtual-environment semantics with real PyRosetta import/version preflight, every metric-affecting canonical PredictionConfig field changes identity, and Boltz version/checkpoint/`no_kernels` are auditable runtime metadata.
- [x] 8.2 Implement the narrow shared identity repair using existing protocol/tool/model/checkpoint observation and validation seams; reject mismatch before commit and as early as existing preflight permits, without adding an executor, generic path abstraction, or generic identity/probing framework.
- [x] 8.3 Add failing recovery regressions proving every exact-scope candidate requires one authoritative `prediction_record` Artifact and one transaction-bound battery/record Evidence plus handoff-ready Evidence, with complete project/workflow/run/plan/task/attempt/transaction/prediction-run/execution-identity correlation; deletion or tampering blocks Critic.
- [x] 8.4 Strengthen `prediction_execution()` formal recovery at its existing boundary without changing owner readiness, L1-L7, Critic, Store schema, or candidate scope.
- [x] 8.5 Add failing retry regressions for missing, active, `COMMITTING`, `COMMITTED`, compensation-conflict, unknown, mismatched, or failure-Evidence-less transactions.
- [x] 8.6 Require a complete, matching, explicitly retryable terminal transaction and formal failure Evidence before publishing a retry plan; preserve prior executions and transaction ownership.
- [x] 8.7 Run focused and full Python suites, compile checks, Architecture Gate, strict OpenSpec, diff check, OpenSpec Verify Change, and final Spec/Standards review; resolve only P0/P1 and stop.

### P1 Closeout Verification

- Focused P1 regressions: `python -m unittest test_prediction_execution_identity test_prediction_transactional test_workflow_boundaries test_planner_bootstrap_prediction` — **36 passed**.
- Relevant Store/transaction/recovery/Worker/Launcher suite — **335 passed, 3 skipped**.
- Full Python suite: `python -m unittest discover` — **725 passed, 4 skipped**.
- Compilation: `python -m compileall -q agents execution prediction_pipeline workflow ...` — **passed**.
- Architecture: `python scripts/architecture_gate.py --baseline architecture_baseline.json` — **passed, 0 new violations on every axis**.
- OpenSpec: `openspec validate approval-gated-initial-prediction-execution --strict` — **passed**.
- Whitespace: `git diff --check` — **passed** (platform line-ending notices only).
- Final Spec review — **PASS, P0/P1=0**. Final Standards review — **PASS, P0/P1=0**.
