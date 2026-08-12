## Context

See `proposal.md` for motivation and `specs/workflow/approval-gated-initial-prediction-execution/spec.md` for observable requirements.

The current Launcher uses `DefaultWorkflowRuntime.run_prediction()` to call `agents.prediction.run()` immediately after Initial Design. That public seam intentionally ingests `<artifacts-root>/<candidate_id>/artifacts.json`, evaluates L1-L7, and writes Prediction records/handoff; it does not run the heavy toolchain. The real scientific executor already exists as the registered `evaluate_new_design_candidates` action. Its Execution handler reuses complete bundles or runs ColabDesign/AF2 plus PRODIGY, Boltz, PyRosetta InterfaceAnalyzer and post-relax, then performs transaction-managed Prediction ingest. The handler already accepts explicit `candidate_scope.candidate_ids`, rejects a scope larger than the approved candidate limit, stages Prediction records/input artifacts/handoff, and commits typed Candidate/State/Artifact/Evidence effects through Worker transaction ownership.

The Planner has a compatibility `plan()` view that recognizes “candidates exist but State has no Prediction handoff” and requests Prediction budget approval, but the canonical immutable plan builder and plan validator currently require an immutable Critic report source. Launcher therefore cannot use the existing approval/Orchestrator/Worker path before Critic without a formal bootstrap plan variant.

The real E2E run also established the compatibility boundary: it has immutable direct Prediction start/completion evidence and a coherent handoff whose 87 records are all `prediction_pending`. PR68 correctly classifies that run as `prediction_execution_incomplete`. This change must not reinterpret or overwrite it.

## Goals / Non-Goals

**Goals:**

- Promote the existing bootstrap “Prediction is next” judgment into one canonical Planner-owned plan accepted by the existing approval, Orchestrator, Worker, and transaction contracts.
- Make the exact committed Initial Design candidate set the only bootstrap task scope.
- Reuse `evaluate_new_design_candidates` as the only scientific executor and recover its result through formal execution proof.
- Bind planning and execution to a path-independent scientific protocol/configuration identity while retaining machine paths as internal execution locators.
- Preserve exact-once recovery and stable causal ordering through Prediction owner readiness before Critic.

**Non-Goals:**

- No new Prediction materializer, tool runner, scheduler, Store table, scientific status, threshold, candidate-selection rule, or automatic approval.
- No changes to L1-L7, `CRITIC_READY_STATUSES`, Critic, or the scientific behavior of existing predictor/enrichment workers.
- No attempt to continue, repair, backfill, or replace the preserved failed Launcher run.
- No requirement that two physically different deployments produce byte-identical nondeterministic GPU output; scientific identity equality means the approved protocol/configuration is equal, not that output identity is path- or machine-derived.

## Decisions

### 1. Planner owns a canonical bootstrap source variant

Add a narrow canonical plan source variant identified by `source.kind = "initial_prediction_bootstrap"`. It binds:

- project ID and approved-content binding;
- Launcher run ID;
- Initial Design invocation ID and completion event ID;
- committed Initial Design transaction ID;
- the exact sorted committed candidate IDs;
- the active Prediction execution identity described below.

The plan has exactly one task:

```text
action: evaluate_new_design_candidates
candidate_scope.candidate_ids: <exact committed set>
candidate_scope.from_task_id: null
parameters:
  reuse_complete_evidence: true
  evidence_mode: reuse_or_generate_full
  predictor_protocol: <active Prediction protocol binding>
  execution_identity: <path-independent scientific identity>
resource_request.candidate_limit: <exact candidate count>
approval.required: execution_budget
```

The Planner derives the source only after validating the Initial Design completion against its `COMMITTED` transaction and authoritative candidate-registration Evidence. Plan identity uses the existing canonical plan identity infrastructure. If the exact set exceeds Planner policy or available approval limits, Planner returns a typed scope/budget blocker; it never slices the set.

The existing Critic-driven plan source remains unchanged. Plan validation becomes a tagged union: the current Critic-report variant retains every existing rule, while the bootstrap variant requires the fields above and forbids Critic report fields, Design tasks, threshold tasks, or recommendations. Existing plan readers that accept only the current schema continue to receive only Critic-driven plans unless explicitly called through the additive bootstrap entry point.

Alternative considered: let Launcher construct a one-task dictionary. Rejected because that would make Launcher a second Planner and bypass canonical plan/approval validation. Alternative considered: synthesize a Critic report. Rejected because it creates false scientific authority.

### 2. Bootstrap planning is additive to Planner, not Critic-driven reasoning

Introduce one public Planner entry point dedicated to the bootstrap condition, implemented in a cohesive bootstrap module rather than adding a second branch through the large Critic-driven plan builder. It consumes formal Design references and current Planner configuration, produces the normal canonical plan envelope, and persists/registers the plan through the same Planner-owned I/O and Evidence seam.

The immutable plan file plus its existing digest-bound `planner_plan` Evidence remain the formal publication seam. Extend that Evidence payload as an explicit source union: new bootstrap events carry `source_kind=initial_prediction_bootstrap` and the Design invocation/completion/transaction and exact candidate-set bindings; current Critic-driven events retain their existing Critic report binding. Bootstrap recovery queries the tagged current source and validates the referenced plan instead of trusting State or diagnostics. Use a shared event-payload contract and the existing generic Evidence publication seam if necessary to avoid a breaking change to the public `EvidenceLogger.planner_plan()` signature.

`planner.plan()` remains a compatibility/read-model API; its “Prediction approval required” judgment can share a small public bootstrap eligibility contract, but the compatibility list is not itself executable authority. The new immutable plan is distinguished in all review and audit output as bootstrap, while a normal plan continues to declare an immutable Critic report as its source.

Alternative considered: change `planner.run(critic_report_path=...)` to make the report optional. Rejected because an optional source would blur two authorities and make existing callers harder to reason about.

### 3. Existing approval, Orchestrator, Worker, and handler contracts remain the execution authority

Launcher passes the immutable bootstrap plan through the existing approval resolver, Orchestrator initializer, transaction recovery check, and Worker drain. It never launches a predictor subprocess. `evaluate_new_design_candidates` remains the only registered handler and retains its current sequence:

```text
reuse complete compatible bundle, otherwise
AF2/ColabDesign + PRODIGY
→ Boltz + PyRosetta InterfaceAnalyzer + post-relax enrichment
→ Prediction ingest/evaluate
→ typed transaction effects
→ Worker staging/validation/commit
```

No scientific code is copied into `workflow`, Planner, or a new materializer. Production changes around the handler are limited to accepting/validating the path-independent execution identity, performing the existing tool preflight against it, and exposing that identity in the formal execution receipt/effects. Tool commands and artifact generation stay where they are.

The bootstrap task uses an explicit candidate scope because its Initial Design occurred before this plan; it does not claim a `from_task_id` dependency. The handler's existing `_prediction_candidate_ids()` path already supports this shape.

Alternative considered: call `scripts/run_prediction_predictors.py` from Launcher after approval. Rejected because Launcher would own subprocess lifecycle and recovery outside Orchestrator/Worker. Alternative considered: add `materialize_prediction_artifacts`. Rejected because the existing action already owns generation plus ingest atomically.

### 4. Prediction scientific identity excludes machine paths

Add one public Prediction-owned execution identity builder/validator beside the existing versioned Prediction protocol contract. The canonical identity contains only scientific configuration:

- Prediction protocol name/version and existing protocol digest;
- ColabDesign pinned source commit;
- declared AF2 model identities and protocol model/seed/recycle configuration;
- Boltz version, model identity, and checkpoint content identity;
- PyRosetta package/protocol version;
- PRODIGY version where it contributes formal evidence;
- canonical configuration digest over the preceding path-free values.

Planner writes the expected identity into the bootstrap source and task parameters. Worker preflight resolves `ExecutionConfig` paths, probes/validates the actual tools and checkpoint using existing Prediction workers/contracts, and records the observed matching identity in the task/process/Prediction receipts. Runtime paths such as Python executables, checkouts, cache directories, checkpoint locations, task directories, artifact roots, and output roots are recorded only in internal execution-locator/process metadata where operationally necessary. They are excluded from plan identity, scientific configuration digest, Prediction status identity, and browser-safe projection.

Because `evaluate_new_design_candidates` is shared, every newly generated task using that action, including later Critic-driven plans, carries the same execution identity parameter. There is no bootstrap-specific Worker contract. Existing completed tasks and plans remain immutable audit history. An old approved or unstarted Prediction task that lacks this identity cannot begin a new scientific invocation after the upgraded Worker contract; Planner must deterministically regenerate a new plan from the same immutable Design or Critic source. The old plan is not edited or silently supplied with current-machine values.

Changing a path alone cannot create a new scientific identity; changing a validated tool version, source commit, model/checkpoint content identity, or protocol configuration does. A new path whose observed identity cannot be proven equal fails preflight rather than being trusted because the file exists.

This is not a new integrity scheme: it composes the repository's existing protocol binding and existing model/checkpoint/tool-version validations into one shared contract. It does not add hashes for files that are not already scientific inputs.

Alternative considered: put `ExecutionConfig` paths into the plan. Rejected because approvals would become host-specific and moving an identical runtime would spuriously change scientific identity. Alternative considered: record only `prediction protocol 1.0`. Rejected because it would not bind tool/model versions already required by the production evidence contract.

### 5. Worker output becomes the new Launcher Prediction authority

New bootstrap runs do not write the direct `prediction_invocation_started` receipt and do not use the deterministic direct-run locator as their completion authority. After the bootstrap Orchestrator run reports the Prediction task succeeded, a new Prediction-owned execution validator consumes a fully specified execution binding:

- bootstrap plan ID/source and approved task ID;
- Orchestrator `run_id`;
- task attempt and transaction ID;
- exact candidate scope and protocol/execution identity;
- formal task output role `prediction_handoff`;
- committed Artifact IDs and Prediction Evidence;
- Prediction `prediction_run_id` and authoritative records referenced by the handoff.

The validator first proves task/transaction/output correlation, then reuses the same public owner readiness logic used by direct Launcher invocation validation. It returns `completed` only if every exact-scope record recomputes to a status in the existing `CRITIC_READY_STATUSES`. A coherent pending battery returns `prediction_execution_incomplete`; missing or contradictory formal bindings retain their specific execution, transaction, correlation, or integrity blocker.

Formal event envelope `run_id` comes exclusively from Worker `TraceContext` and remains the Orchestrator identity. The domain run stays `prediction_run_id` in payloads. Existing TRACE_KEYS conflict validation remains generic; no Prediction exception or dual-written `run_id` is introduced.

Alternative considered: have Launcher scan `execution_root/artifacts`. Rejected because paths are not formal completion proof. Alternative considered: teach the direct invocation validator to accept either arbitrary files or task outputs. Rejected because the two start authorities differ; share only the owner record/readiness validation and keep correlation adapters explicit.

### 6. Launcher has a pre-Critic bootstrap state machine, not a second workflow authority

Refactor the pre-Critic coordination into a focused module if needed to keep `workflow/service.py` within the Engineering Standard size boundary. The causal sequence for new runs is:

```text
Research completed
→ Initial Design completed and committed
→ bootstrap plan absent: create idempotently (launch/resume only)
→ bootstrap plan present, approval absent: awaiting_approval
→ approval present, Orchestrator absent: initialize
→ run ready: Worker drain
→ run/task terminal: inspect formal task/transaction output
→ Prediction owner readiness completed
→ invoke/inspect Critic
→ build the existing Critic-driven plan
→ normal approval/Orchestrator continuation
```

`status` remains read-only. If it observes the narrow crash window after Design commit but before bootstrap plan persistence, it reports the formal bootstrap-planning boundary as pending; `resume` may idempotently create the plan. Once the plan exists, launch/status/resume all project the same approval or execution state. Completing the bootstrap Orchestrator run is not overall Launcher completion: Launcher returns to Prediction owner inspection, then Critic and the normal Planner flow.

The diagnostic may mirror bootstrap plan/run/task and Prediction identities, but recovery always re-derives them from Planner Evidence, approval, Orchestrator/task state, transaction, Store Artifacts/Evidence, and task output. It does not become a multi-plan authority.

### 7. Recovery is exact-once and fail-closed

Recovery order for the pre-Critic phase is:

1. Check for any existing direct Launcher Prediction receipt for the deterministic legacy correlation. If present, use the existing direct validator and never create a bootstrap plan for that run.
2. Validate Initial Design completion and committed exact candidate scope.
3. Resolve at most one bootstrap plan for that source. Missing plan permits only launch/resume to create it; conflicting plans block.
4. Resolve approval and Orchestrator by existing immutable bindings.
5. Run transaction recovery before Worker drain.
6. Treat succeeded task plus committed transaction and validated outputs as completed work; never run it again.
7. Treat active/claimed attempts as active, and partial/unknown attempts or unresolved transactions as blockers; do not create a new attempt automatically.
8. Validate Prediction owner readiness before Critic.

An ordinary task failure remains terminal for this bootstrap plan. Operators fix the deployment or scientific input and start a newly approved run/plan through the normal contract; Launcher does not silently retry expensive tools.

### 8. Compatibility and migration are forward-only

The direct `agents.prediction.run()` public API and non-Launcher callers are unchanged. Existing Critic-driven plan source semantics and completed plan/approval history remain valid. The canonical plan validator gains an additive tagged bootstrap variant, and all newly generated Prediction execution tasks carry the shared execution identity. Old plans are not rewritten; an unstarted task lacking that identity is read-compatible but not execution-compatible and must be replanned from its immutable source.

Existing Launcher runs with direct Prediction evidence remain on the direct correlation path, including the preserved E2E run blocked on pending artifacts. The presence of that formal start evidence is the durable migration discriminator, not a diagnostic version or current environment flag. New runs created after deployment take the bootstrap path and never write direct invocation start evidence.

### 9. Failed bootstrap execution has an explicit retry plan, never an automatic task retry

Use the existing immutable plan, approval, Orchestrator, and Worker contracts rather than reopening a failed task attempt. A bootstrap task failure remains terminal under ordinary `launch`, `status`, and `resume`. Add one explicit Launcher resume option for operator intent, for example `resume --retry-bootstrap-prediction`, which is accepted only when:

- the current bootstrap task is formally terminal failed;
- its transaction is rolled back/terminal and transaction recovery is clean;
- no task is active or ambiguously claimed;
- no Critic-ready Prediction execution already exists;
- the original Initial Design completion, committed transaction, project approval, and exact candidate set still validate.

The explicit request asks Planner to create one new immutable bootstrap plan. Its source repeats the same Design authority and adds a retry binding containing the prior plan, Orchestrator run, task/attempt, terminal failure, and monotonic retry index. The task candidate scope is byte-for-byte/canonically the same exact set. The new plan uses the current validated Prediction execution identity; a repaired path with the same scientific runtime leaves identity equal, while a real tool/model/protocol change produces a different identity and is visible in the new approval contract.

The command returns `awaiting_approval`; it never fabricates approval or starts science in the same step. A later ordinary resume with approval uses the existing Orchestrator/Worker path. Planner plan identity and the tagged `planner_plan` Evidence make repeated identical retry requests idempotent. A second retry cannot be generated until the current retry plan itself reaches a formal terminal failure and the operator explicitly requests again.

This preserves Research and Initial Design work: retry planning reads the original formal Design completion and transaction and does not invoke either agent. It also preserves the failed execution as immutable evidence rather than resetting task status or reusing an attempt ID.

Alternative considered: reset the failed task or call Worker with a higher attempt number in the same Orchestrator run. Rejected because it mutates terminal authority and makes approval scope ambiguous. Alternative considered: ordinary resume automatically create a new plan after deployment repair. Rejected because runtime repair is not formal operator intent and expensive science requires reapproval.

## Risks / Trade-offs

- **[The plan schema previously assumed every canonical plan has a Critic source]** → Implement an explicit tagged union with exhaustive validation; leave the existing Critic variant unchanged and add cross-variant negative tests.
- **[Launcher now encounters two Planner/Orchestrator cycles]** → Name and validate the bootstrap source distinctly, treat its completed run as a pre-Critic boundary rather than terminal workflow success, and keep the later Critic-driven plan unchanged.
- **[A very large Design candidate set exceeds practical budget]** → Return an explicit budget/scope blocker. Do not truncate or add selection policy in this change; any future shortlist policy requires its own approved scientific contract.
- **[Tool identity cannot be known until the deployment is probed]** → Plan binds the protocol-declared expected identity; Worker preflight proves the resolved deployment matches before expensive science and records observed identity in receipts.
- **[A path-independent identity could accidentally omit a scientifically relevant option]** → Build it from the existing versioned Prediction protocol and existing tool/model validation constants, characterize every current executor input, and reject unknown identity fields until a protocol version is updated.
- **[Worker succeeds but Launcher crashes before Critic]** → Recover from task, transaction, Artifact/Evidence, and output proof and skip all scientific execution.
- **[Legacy direct and bootstrap evidence coexist]** → Presence of direct start evidence wins and blocks bootstrap creation; tests cover contradictory mixed state fail-closed.
- **[Operators may repeatedly request retries]** → Retry creation is idempotent for the latest terminal failure, requires one new plan-bound approval per execution, and cannot advance while a retry plan is awaiting approval, active, ambiguous, completed, or not terminal failed.

## Migration Plan

1. Characterize the existing direct Launcher Prediction path, Planner compatibility bootstrap judgment, canonical Critic-plan validation, explicit candidate-scope handler path, Worker transaction output, and direct Prediction owner readiness.
2. Add the shared path-independent Prediction execution identity contract and validate the current executor/profile without changing scientific algorithms.
3. Add the Planner bootstrap source/plan variant and extend canonical plan/approval validation as a tagged union.
4. Add formal task-output-based Prediction execution validation and Launcher pre-Critic bootstrap coordination.
5. Add operator-explicit failed-execution retry planning and recovery without reopening old tasks or rerunning Research/Initial Design.
6. Run focused failure/recovery tests, full Python suite, Architecture Gate, strict OpenSpec, diff check, and Spec/Standards review.
7. Deploy Planner/Launcher/Prediction owner readers and writers together. Configure and preflight the complete existing Prediction toolchain before creating a new acceptance run.

Rollback is code-only for new runs that have not started bootstrap execution. Bootstrap plans, approvals, task runs, transactions, Artifacts, and Evidence already published remain immutable audit records. A rollback that cannot understand a new bootstrap plan must fail closed; it must not reinterpret it as a Critic-driven plan or direct Prediction invocation. Old direct runs require no migration.
