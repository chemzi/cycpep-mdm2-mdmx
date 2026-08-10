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
- [ ] 6.4 Run the repository `code-review` skill against the integration merge base and the mandatory Strict Code Review, address in-scope findings, report the Standards and Spec results, and require the Strict Code Review score to be at least 85 before merge.
