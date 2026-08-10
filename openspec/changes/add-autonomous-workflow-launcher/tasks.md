## 1. Characterize and complete the missing public boundaries

- [x] 1.1 Add characterization tests for current project approval, Research, Design route, Prediction resume, Critic, Planner, approval, Orchestrator, Worker drain, Store query, and transaction recovery behavior used by the Launcher.
- [x] 1.2 Add the public `agents.research.run_with_receipt(..., correlation=...)` wrapper, pre-side-effect `research_invocation_started` event, post-commit `research_completion_receipt` event, Research-owned validator, and Store-backed lookup bound to `research_invocation_id`, `launcher_run_id`, `project_id`, approved-content identity, and formal Research Evidence IDs while leaving `run(...)` unchanged.
- [x] 1.3 Add the fixed `launcher_<uuid-payload> -> design_initial_<uuid-payload>` mapping and a Design-owned initial invocation/validator contract that durably writes bound `design_initial_invocation_started` Evidence before scientific side effects and a correspondingly bound `design_initial_completion` covering jobs, candidates, Artifacts, and Evidence.
- [x] 1.4 Make the Design initial contract reject unsupported or ambiguous route/job selection before scientific execution and expose partial-without-completion as a structured recovery blocker rather than retrying.
- [x] 1.5 Add compatibility tests proving legacy Research and Design public interfaces and existing Evidence readers behave unchanged when launcher correlation inputs/new event types are absent or ignored.
- [x] 1.6 Add the additive Prediction invocation-correlation input and Prediction-owned recovery validator over its Store-backed start receipt, exact run manifest, input snapshot, handoff, and completion Evidence; require correlation fields for Launcher runs while preserving legacy callers/runs without them.

## 2. Diagnostic and inspection contracts

- [x] 2.1 Define the versioned launcher diagnostic model, opaque reference model, structured error model, and browser-safe result projection without introducing formal task/run/transaction/candidate state.
- [x] 2.2 Implement deterministic global diagnostic-root resolution, direct validated `launcher_run_id` lookup, atomic persistence, and per-run locking using existing JSON/path infrastructure and no directory scanning or new hash mechanism.
- [x] 2.3 Implement bounded error sanitization that excludes secrets, sensitive internal paths, tracebacks, and full stdout/stderr dumps from browser-facing output.
- [x] 2.4 Implement read-only formal boundary inspection for correlated Research receipts, initial Design receipts, Prediction handoffs, Critic reports, Planner plans, approval bindings, Orchestrator status, Store transactions, Evidence, and Artifacts; reject missing, conflicting, or partial evidence with stable blocker codes.
- [x] 2.5 Add authority-boundary tests proving edited, stale, or deleted diagnostics cannot change formal state or override Store, Evidence, Transaction, Planner, or Orchestrator truth.
- [x] 2.6 Persist the initial diagnostic project binding before Research; prove an initial-write failure causes zero scientific side effects and that resume may start Research only when no correlated `research_invocation_started` Evidence event exists.
- [x] 2.7 Implement the fixed non-hash Prediction namespace mapping and a Prediction-owned pre-invocation seam that atomically persists a Store-backed `prediction_invocation_started` receipt binding the original resolved `(prediction_run_root, prediction_run_id)` locator and expected inputs before Prediction; abort without calling Prediction when receipt persistence fails, and on resume use that receipt rather than diagnostic or ambient environment values.

## 3. Launcher coordination and resume

- [x] 3.1 Implement the fixed launch sequence over public seams: approval validation, durable initial diagnostic persistence, Research, initial Design, Prediction, Critic, Planner, and the explicit approval pause.
- [x] 3.2 Implement the approved continuation sequence using the existing Planner approval validation, Orchestrator initialization, `drain_run`, and Orchestrator status contracts, with no selfcheck bootstrap or automatic approval.
- [x] 3.3 Implement fail-fast boundary execution so Research, Design, Prediction, Critic, Planner, approval, Orchestrator, and Worker failures stop later calls, preserve committed data, and record structured diagnostics.
- [x] 3.4 Implement formal-first resume in reverse boundary order, including exact `project_id` and approved-content binding checks, transaction recovery checks, approval attachment, Orchestrator status handling, and fail-closed behavior for ambiguous pre-Orchestrator boundaries.
- [x] 3.5 Implement idempotent terminal and pause handling so repeated resume does not duplicate completed science, approvals, plans, runs, tasks, Evidence, or Artifacts.

## 4. CLI and operator contract

- [x] 4.1 Add `python -m workflow launch --project <path>` with one browser-safe JSON document on stdout and documented exit codes.
- [x] 4.2 Add read-only `python -m workflow status --launcher-run <id>` that revalidates formal references and performs no scientific or formal transition.
- [x] 4.3 Add `python -m workflow resume --launcher-run <id> [--approval <path>]...` with explicit approval inputs and no approval bypass.
- [x] 4.4 Document CLI examples, possible outcomes, diagnostic location/retention, approval handoff, recovery blockers, and the diagnostic-versus-formal-authority boundary.

## 5. Focused acceptance and recovery tests

- [x] 5.1 Add a dependency-injected happy-path test covering approved project -> Research -> initial Design -> Prediction -> Critic -> Planner -> `awaiting_approval` with real contract-shaped artifacts and no synthetic Critic bootstrap.
- [x] 5.2 Add boundary-isolation tests for Research, Design, Prediction, Critic, and Planner failures, asserting non-zero exit, no later call, preserved committed data, and structured diagnostic attribution.
- [x] 5.3 Add approval tests for the intentional `awaiting_approval` pause, valid approval continuation, stale/wrong approval rejection, budget enforcement, and awaiting further approval after partial execution.
- [x] 5.4 Add Worker tests for task failure and formal `completed`, `completed_required`, `blocked`, `failed`, and `awaiting_approval` outcome projection.
- [x] 5.5 Add a fault-injection test where formal transaction/task/run completion succeeds and the following launcher report write fails; verify resume observes the committed formal state and never reruns the scientific action.
- [x] 5.6 Add unresolved transaction/recovery tests that return a structured blocker and never invoke a GPU/scientific handler.
- [x] 5.7 Add repeated-resume idempotency tests across completed, failed, blocked, and approval-waiting states.
- [x] 5.8 Add Design start-receipt persistence-failure and durable-start-without-completion tests plus other ambiguous pre-Orchestrator recovery tests, proving zero Design side effects before a durable start and fail-closed behavior after a start without directory scans, `State.phase`, stdout parsing, or journal authority.
- [x] 5.9 Add sanitization tests for secrets, internal sensitive paths, multiline process output, and tracebacks in CLI/browser-facing results.
- [x] 5.10 Add failure-isolation and fault-injection tests for approval attachment, Orchestrator initialization, Orchestrator status reads, initial diagnostic creation, and diagnostic updates outside the post-commit case.
- [x] 5.11 Add Research crash tests for no start event, durable start without completion, durable completion before launcher bookkeeping, and repeated resume without Research reinvocation.
- [x] 5.12 Add resume-binding tests proving changed `project_id` or changed approved content returns a blocker before formal recovery or scientific execution.
- [x] 5.13 Add identity-contract tests proving `prediction_invocation_id`, `prediction_run_id`, and formal Orchestrator `run_id` are pairwise distinct; Prediction identities never populate or alias `formal_trace.run_id`; and that field is null before Orchestrator initialization.
- [x] 5.14 Add Prediction bookkeeping-crash recovery coverage: formal Prediction completion followed by diagnostic-write failure, then resume with the same reconstructed identities, unchanged Prediction invocation count, no duplicate formal artifacts/Evidence, repaired diagnostics, and continuation from Critic.
- [x] 5.15 Add partial/conflicting Prediction recovery coverage asserting a stable structured blocker, non-zero exit, and zero subsequent Prediction or Critic calls without scanning, `State.phase`, stdout, or journal completion claims.
- [x] 5.16 Add Initial Design bookkeeping-crash recovery coverage: durable `design_initial_completion` followed by diagnostic-write failure, then resume with the same reconstructed Design identity, unchanged Design/GPU invocation count, repaired diagnostics, and continuation from Prediction.
- [x] 5.17 Add Prediction locator tests covering Prediction-owned start-receipt persistence failure, edited/stale diagnostic locator metadata, and changed `CYCPEP_PREDICTION_ROOT`/`NP_DATA`, proving no Prediction call on receipt-write failure and exclusive reuse of the Store-backed original locator on resume.
- [x] 5.18 Add legacy Prediction manifest compatibility tests proving non-Launcher expected/persisted manifests omit correlation keys entirely, add no `null` fields, and preserve existing strict-equality `--resume` behavior for historical runs.

## 6. Verification and review gates

- [x] 6.1 Run the focused Launcher, Agent seam, approval, Orchestrator, Worker, transaction, recovery, and Store tests and record the commands/results in the change handoff.
- [x] 6.2 Run the full applicable Python test suite, configured lint/type checks, and `scripts/architecture_gate.py`; resolve regressions within scope and report unrelated failures separately.
- [x] 6.3 Run strict OpenSpec validation and `$openspec-verify-change` against the approved artifacts.
- [x] 6.4 Run the repository `code-review` skill against the integration merge base and the mandatory Strict Code Review, address in-scope findings, report the Standards and Spec results, and require the Strict Code Review score to be at least 85 before merge.

## 7. Merge-blocker characterization and remediation

- [x] 7.1 Add characterization tests on the current PR head for Prediction-first recovery ordering and the contradictory-state matrix; record that blocked/partial Prediction currently permits forbidden downstream inspection or continuation before changing production code.
- [x] 7.2 Add characterization tests for diagnostic failure/trace preservation and explicit owner-proven failure clearing, including repeated Worker-failed status/resume calls.
- [x] 7.3 Add characterization tests for read-only status transaction recovery, proving unresolved transaction identifiers are retained and no mutating recovery, task claim, or Worker drain occurs.
- [x] 7.4 Add characterization tests for current-run Critic correlation and legacy compatibility: unrelated broken legacy history, broken explicit current report, and conflicting current records.
- [x] 7.5 Add characterization tests rejecting cross-project Research Evidence references.
- [x] 7.6 Audit the official Data Layer/ProjectContext path contract and add characterization tests for custom data/Evidence/database paths plus restoration after exceptions, without Launcher-owned environment parsing or path guessing.
- [x] 7.7 Implement Prediction-first causal boundary ordering and the unified contradictory-state recovery matrix without consulting downstream authority while upstream state is incomplete or ambiguous.
- [x] 7.8 Make diagnostic observations and plan-trace enrichment merge non-destructively, and add an explicit failure-clear seam guarded by formal owner validation.
- [x] 7.9 Add and use the minimal public read-only transaction/recovery inspector for `status`; retain the existing mutating recovery seam only for explicit resume/Worker continuation.
- [x] 7.10 Add `prediction_run_id` to new Critic review Evidence and implement current-run-first filtering with safe legacy validation and fail-closed current ambiguity.
- [x] 7.11 Scope Research receipt Evidence lookup and reference validation to the expected project.
- [x] 7.12 Make Launcher runtime binding consume one official resolved ProjectContext/ProjectPaths contract across Agents and Store, fail closed on conflicting inputs, and restore legacy globals on every exit.
- [x] 7.13 Add the complete adversarial recovery matrix covering Prediction/downstream contradictions, unresolved transactions, terminal Worker failure, current versus historical Critic corruption, diagnostic/formal disagreement, owner-proven recovery, and cross-project Research references.

## 8. Remediation verification and independent review

- [x] 8.1 Run all new regression tests plus focused Launcher, Research, Design, Prediction, Critic, Planner, Orchestrator, Execution, transaction, recovery, Store, and Workbench V2 tests.
- [x] 8.2 Run the full applicable suite, configured lint/type checks, Architecture Gate, and `git diff --check`.
- [x] 8.3 Run `openspec validate add-autonomous-workflow-launcher --strict` and `$openspec-verify-change`; reconcile tasks and artifacts with the implemented contracts.
- [x] 8.4 Run fresh independent Spec, Standards, and Strict Code Reviews from the integration merge base, require a new score of at least 85 with no P0/P1, and keep PR #62 Draft without merging.

## 9. Fresh-review blocker remediation

- [x] 9.1 Add RED tests proving `ProjectPaths` owns the exact database path, Research rejects projectless cross-project references, and read-only recovery distinguishes DB-only unresolved, live-owner, and formally closed marker states without mutation.
- [x] 9.2 Add `database_path` and the documented runtime-path constructor to the public `ProjectContext` contract; bind the resolved database path exactly and never re-resolve it inside Launcher coordination.
- [x] 9.3 Remove projectless Research Evidence fallback from launcher-correlated completion validation while preserving legacy non-Launcher Research execution.
- [x] 9.4 Complete the owner-side read-only recovery inspector over Store rows, marker state, owner liveness, and Orchestrator closure using shared recovery parsing/classification primitives.
- [x] 9.5 Split approval resolution, Orchestrator initialization, recovery gating, Worker drain, and formal outcome projection so no new service function combines the full workflow continuation responsibilities.
- [x] 9.6 Add bounded operational logging for catch-all failures that cannot be durably journaled, without exposing raw traces, secrets, paths, or stdout in browser output.
- [x] 9.7 Re-run focused/full/architecture/frontend/OpenSpec gates and repeat all three independent reviews from the updated fixed point.

## 10. Latest-integration merge-readiness remediation

- [x] 10.1 Merge `origin/integration/data-integrity-transaction` into the shared PR branch without force-push, preserve Planner compute-aware metadata and Frontend V2 additions, inspect the merge result, and run `git diff --check`.
- [ ] 10.2 Add RED characterization for `running`/`pending` transaction recovery gating, live-owner preservation, stale-owner recovery, post-recovery Orchestrator re-read, unresolved blocker projection, and zero duplicate claim/scientific action.
- [ ] 10.3 Implement formal transaction inspection for every `ready`/`running`/`pending` resume path, delegate stale-owner mutation to `recover_transactions`, preserve live owners, and re-read formal state before drain or outcome projection.
- [ ] 10.4 Add RED characterization and implementation for owner-proven clearing of a resolved `transaction_recovery_unresolved` diagnostic plus non-destructive Orchestrator/formal trace merging before recovery inspection.
- [ ] 10.5 Add a durable internal runtime locator binding before science; make later status/resume reconstruct the original ProjectContext across environment drift, fail closed when it cannot be restored, and keep internal paths out of browser-safe and generic Evidence output.
- [x] 10.6 Complete Critic legacy-history classification so unrelated broken history cannot block a current completed Prediction with no Critic record, while explicit-current and possibly-current unverifiable records remain fail-closed.
- [x] 10.7 Add Planner integration compatibility coverage proving Launcher inspection, immutable-plan validation, approval binding, and Orchestrator initialization preserve current `decision_metadata`, compute estimates, budget metadata, and plan identity.
- [ ] 10.8 Run the complete focused Launcher, Critic, Research, Prediction, Design, transaction/recovery, Worker, Planner, Orchestrator, Store, and Workbench V2 suites on the merged baseline.
- [ ] 10.9 Run the full Python and Frontend suites, lint, typecheck, frontend build, Architecture Gate, compileall, and `git diff --check` on the final fixed point.
- [ ] 10.10 Run strict OpenSpec validation and `$openspec-verify-change`, then fresh independent Spec, Standards, and Strict Code Reviews against the latest integration merge base; require behind=0, P0=0, P1=0, and Strict score >=85 while keeping PR #62 Draft and unmerged.
