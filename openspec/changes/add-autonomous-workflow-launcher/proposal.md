## Why

The repository has public entry points for individual workflow stages and a proven Planner-to-Execution composition, but it has no single, reviewable entry point that can drive an approved project, stop safely at approval boundaries, and resume from formal persisted state after a process failure. Workflow Launcher is promoted to P0 so operators no longer need ad hoc scripts that can lose trace context, repeat expensive scientific work, or accidentally become a second workflow authority.

## What Changes

- Add a thin `workflow` package with `launch`, `status`, and `resume` CLI commands for one approved project.
- Coordinate existing public Research, Design, Prediction, Critic, Planner, Orchestrator, ExecutionWorker, Store, Evidence, Trace, and Transaction seams without reimplementing their algorithms or state ownership.
- Add the minimum missing public boundary contracts needed for durable Research correlation/completion, a deterministically reconstructable initial Design invocation, and authoritative boundary inspection. These contracts remain owned by the relevant Agent/Store layer; the Launcher does not infer completion from filenames, stdout, `State.phase`, or its journal.
- Require the initial launcher diagnostic record to be durably persisted before any scientific side effect, so a created `launcher_run_id` is always recoverably bound to its approved project content before Research begins.
- Keep Prediction invocation identity and Prediction pipeline `run_id` distinct from the formal Orchestrator `run_id`, make both Prediction identities deterministically reconstructable, and have Prediction durably bind the original exact run locator before scientific side effects so recovery cannot drift with the current environment or trust diagnostic metadata as authority.
- Bind resume to the original `project_id` and approved-content identity and fail closed when either has changed.
- Persist a versioned launcher diagnostic report containing correlation identifiers, completed call boundaries, structured failures, and references to formal artifacts/evidence/runs/transactions. The report is diagnostic only and cannot declare formal task, run, candidate, or transaction success.
- Fail fast with a non-zero exit code on errors; preserve committed data; stop at `awaiting_approval`; and fail closed when formal state cannot prove that resuming is safe.
- Reuse the established `Planner.run -> approval artifact -> Orchestrator.initialize -> ExecutionWorker.drain_run -> Orchestrator.status` composition after a valid approval artifact is supplied. The selfcheck-only synthetic Critic bootstrap behavior is explicitly excluded.
- Add focused failure, recovery, idempotency, authority-boundary, CLI, and end-to-end tests for the requested workflow stages.
- Enforce upstream-first pre-Planner recovery: Prediction ambiguity or incompletion is resolved or blocked before Critic and Planner records are inspected or invoked, so stale downstream success can never override an upstream formal blocker.
- Preserve diagnostic failures and accumulated formal trace identifiers across ordinary observations; clear a recorded failure only through an explicit operation after an owning formal contract proves recovery.
- Add a minimal owner-side read-only transaction recovery inspection for `status`, while retaining the existing mutating recovery path only for explicit continuation.
- Correlate new Critic review Evidence to its Prediction run before opening report artifacts, scope Research receipt references to the expected project, and require one explicit ProjectContext path set across Launcher and all formal stores.
- Gate every resumable Orchestrator state (`ready`, `running`, or `pending`) through formal transaction inspection. Status remains read-only; resume preserves a live owner, invokes the existing recovery owner only for stale unresolved work, and re-reads Orchestrator before deciding whether to drain or return.
- Durably bind the resolved data, Evidence, database, and approved-project locators in the initial internal diagnostic before science, then reuse that exact locator set for later `status` and `resume` commands so ambient path drift cannot select a different formal Store. These locators remain internal location metadata and never prove workflow completion.
- Keep diagnostic lookup independent from formal-runtime selectors such as `NP_DATA`: an explicit Launcher diagnostics root is operator configuration, otherwise the repository diagnostics root is stable. Persist the original runtime locator as a write-once DiagnosticStore binding; the mutable journal may mirror it but cannot redirect it to another absolute Store.
- Clear a recorded transaction blocker only after the formal transaction owner proves recovery clean, and merge current Orchestrator identifiers into diagnostics before recovery inspection without discarding task, attempt, or transaction identifiers.
- Complete legacy Critic isolation for a current Prediction with no Critic result: unrelated broken history is ignored, while possibly-current unverifiable or explicitly current broken records remain fail-closed.
- Preserve the latest Planner immutable plan, `decision_metadata`, compute estimates, budget metadata, approval binding, and Orchestrator initialization contract without reconstructing an older plan shape.
- No breaking change is intended. Existing Agent CLIs and Python entry points remain supported; new optional correlation inputs must preserve existing behavior when omitted.

## Capabilities

### New Capabilities

- `workflow/autonomous-launcher`: Defines approved-project launch, diagnostic status, approval-aware continuation, authoritative resume, failure reporting, and the prohibition on shadow workflow state.

### Modified Capabilities

None.

## Impact

- **Behavior:** adds an operator-facing coordination path; does not change scientific algorithms, thresholds, Planner decisions, Orchestrator authority, transaction semantics, or worker scheduling.
- **Public interfaces:** adds `python -m workflow launch|status|resume`; adds only narrow optional public correlation/boundary seams where the audit found no safe existing interface. Exact additions are governed by the design and spec.
- **Data formats:** adds a versioned launcher diagnostic JSON format and a directly addressed, write-once internal locator-binding sidecar containing the originally resolved runtime locators; additive `research_invocation_started`, `research_completion_receipt`, `design_initial_invocation_started`, `design_initial_completion`, and `prediction_invocation_started` payload contracts inside the existing Store-backed `EvidenceEvent` envelope; additive Prediction invocation-correlation fields in the existing Prediction manifest/handoff/Evidence contracts; and an optional `prediction_run_id` correlation field on new `critic_review` Evidence. Internal paths are excluded from browser-safe output. The sidecar is location metadata only and is not a workflow database or formal completion record. No Store table, Evidence envelope, plan, approval, Orchestrator run, task, candidate, artifact, or transaction schema is redesigned.
- **Migration:** none. Existing projects, Evidence readers, Prediction runs, and workflow artifacts remain valid. Launcher correlation fields are mandatory only for launcher-correlated Prediction runs; legacy/non-Launcher manifests omit those keys entirely rather than adding `null`, preserving strict manifest-equality resume behavior. Readers that do not understand new Evidence event types continue to ignore them. Diagnostics are additive and may be deleted without changing formal workflow state.
- **Affected areas:** new `workflow/` coordination and CLI modules, narrow public Agent/Store boundary additions, tests, and operator documentation.
- **Legacy paths retained:** individual Agent CLIs, `scripts/run_execution_selfcheck.py`, and direct public Python seams remain available. The selfcheck script remains an isolated validation tool and is not used as Launcher authority.
- **Non-goals:** frontend controls, automatic code repair, unlimited retry, threshold mutation, approval bypass, a new scheduler/database/state machine, remote GPU redesign, Orchestrator authority migration, Store schema redesign, Agent rewrites, or Initial Design child-level provenance hardening beyond the already approved invocation receipts.
