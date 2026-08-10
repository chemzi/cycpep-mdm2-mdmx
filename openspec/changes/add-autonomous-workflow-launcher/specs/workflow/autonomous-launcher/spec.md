## Purpose

Provide one approval-aware, observable, and recoverable entry point for coordinating the repository's existing scientific workflow without creating a second owner of formal workflow state.

## ADDED Requirements

### Requirement: Approved project launch entry point
The system SHALL provide `python -m workflow launch --project <approved-project.json>`. It SHALL validate the project through the existing project approval contract before invoking a scientific stage, create a unique `launcher_run_id`, and return one structured JSON result.

#### Scenario: Approved project starts
- **WHEN** an operator launches a project whose current content is covered by its formal approval
- **THEN** the Launcher returns its `launcher_run_id` and begins at the first boundary not already proven complete by formal state

#### Scenario: Project approval is absent or stale
- **WHEN** the supplied project is unapproved or its approved content no longer matches the current project content
- **THEN** the Launcher invokes no Research, Design, Prediction, Critic, Planner, Orchestrator, or Worker action, records a structured validation failure, and exits non-zero

### Requirement: Initial diagnostic persistence precedes science
After validating project approval and allocating `launcher_run_id`, the Launcher SHALL durably persist the initial diagnostic record, including the project identity and approved-content binding, before invoking Research or causing any other scientific side effect.

#### Scenario: Initial diagnostic persistence succeeds
- **WHEN** an approved launch allocates a `launcher_run_id` and the initial diagnostic record is durably written
- **THEN** the Launcher may invoke Research using that persisted correlation and project binding

#### Scenario: Initial diagnostic persistence fails
- **WHEN** the initial diagnostic record cannot be durably written
- **THEN** the Launcher exits non-zero and invokes no Research, Design, Prediction, Critic, Planner, Orchestrator, Worker, GPU, or other scientific action

#### Scenario: Crash after initial diagnostic but before Research
- **WHEN** the process crashes after the initial diagnostic is durable but before a correlated `research_invocation_started` Evidence event exists
- **THEN** resume revalidates the original project binding and may begin Research because formal Evidence proves the Research invocation never started

### Requirement: Existing workflow authorities are reused
The Launcher SHALL coordinate existing public Agent and execution contracts. It MUST NOT implement scientific algorithms, Planner decisions, approval validation, Orchestrator task transitions, worker scheduling, transaction transitions, candidate ownership, evidence ownership, or Store persistence semantics.

#### Scenario: Workflow reaches Planner
- **WHEN** Research, initial Design, Prediction, and Critic complete through their public contracts
- **THEN** the Launcher passes the real Critic report to the public Planner contract and records the returned formal plan identifiers

#### Scenario: Approved plan executes
- **WHEN** a valid approval artifact exists for the current immutable plan
- **THEN** the Launcher uses the public Orchestrator initialization, Worker drain, and Orchestrator status contracts and reports the formal run outcome

#### Scenario: Selfcheck bootstrap is not reused
- **WHEN** the Launcher needs a Critic input for Planner
- **THEN** it uses a Critic report produced from the real Prediction handoff and never constructs or accepts the selfcheck-specific synthetic Critic bootstrap as workflow evidence

### Requirement: Research has durable correlation and completion
Research SHALL expose an additive public invocation contract that binds one invocation to `launcher_run_id`, `project_id`, and the current approved-content identity. It SHALL durably record a `research_invocation_started` event in the existing Store-backed Evidence envelope before any Research side effect and SHALL durably record a `research_completion_receipt` event with its Research Evidence IDs only after Research's formal outputs are committed. The start event SHALL be the authority that the correlated invocation began, and the completion receipt SHALL be the sole proof that it completed.

#### Scenario: Correlated Research starts
- **WHEN** the Launcher requests Research for a validated correlation
- **THEN** Research durably records `research_invocation_started` with the invocation and project bindings before producing any scientific or formal side effect

#### Scenario: Correlated Research completes
- **WHEN** Research formal outputs are committed for a launcher-correlated invocation
- **THEN** Research durably records and returns the `research_completion_receipt` Evidence event containing the invocation identity, `launcher_run_id`, `project_id`, approved-content binding, and relevant Evidence IDs

#### Scenario: Crash before Research completion receipt
- **WHEN** `research_invocation_started` is durable but the correlated completion receipt is absent, whether or not another Research side effect is observable
- **THEN** resume reports an ambiguous Research blocker and does not automatically rerun Research or advance to Design

#### Scenario: Crash after Research completion receipt
- **WHEN** the Research completion receipt is durable but the process crashes before the launcher diagnostic records the completed boundary
- **THEN** resume validates the formal receipt, skips Research, repairs the diagnostic observation, and continues to initial Design

#### Scenario: Repeated resume after Research completion
- **WHEN** resume is called repeatedly with the same project binding after a valid correlated Research completion receipt exists
- **THEN** Research is not reinvoked and the same formal receipt remains the completion authority

### Requirement: Safe initial scientific boundary
The initial Design boundary SHALL be exposed by a public Design-owned contract whose `design_invocation_id` is deterministically reconstructed from the durable `launcher_run_id` through the fixed non-hash mapping `launcher_<uuid-payload> -> design_initial_<uuid-payload>`. Before any scientific, GPU, candidate, or artifact side effect, the contract SHALL durably record `design_initial_invocation_started` in the existing Store-backed Evidence envelope, bound to `design_invocation_id`, `launcher_run_id`, `project_id`, `approved_content_binding`, and the materialized job/config identity. It SHALL record a correspondingly bound `design_initial_completion` after the formal outputs complete. The Launcher MUST NOT select routes, seeds, target jobs, locate completion, or validate recovery by scanning CandidateIndex, directories, timestamps, or journal completion claims.

#### Scenario: Initial Design start persistence fails
- **WHEN** `design_initial_invocation_started` cannot be durably persisted
- **THEN** Design exits before scientific/GPU/candidate/artifact side effects and Prediction is not called

#### Scenario: Initial Design completes
- **WHEN** the Design-owned initial invocation finishes successfully
- **THEN** `design_initial_completion` records `design_invocation_id`, `launcher_run_id`, `project_id`, `approved_content_binding`, executed jobs, candidate IDs, relevant Artifact IDs, and Evidence IDs needed for Design-owned authoritative validation

#### Scenario: Initial Design is partially observable
- **WHEN** `design_initial_invocation_started` is durable but the Design-owned completion record is absent or invalid
- **THEN** launch or resume returns `design_recovery_ambiguous`, fails closed, and does not automatically rerun the scientific Design action

#### Scenario: Initial Design completion survives launcher bookkeeping crash
- **WHEN** `design_initial_completion` is durable but the following launcher diagnostic update fails or the process crashes
- **THEN** resume reconstructs the same `design_invocation_id`, uses the Design-owned validator to prove completion and project binding, does not call Design or GPU work again, repairs the diagnostic observation, and continues from Prediction

#### Scenario: Initial Design recovery is ambiguous
- **WHEN** the Design-owned validator finds partial, conflicting, differently bound, or non-unique formal records for the reconstructed `design_invocation_id`
- **THEN** resume returns a stable structured blocker with non-zero exit and invokes neither Design nor Prediction

### Requirement: Approval is an explicit boundary
The Launcher SHALL preserve the existing Planner approval semantics. It MUST NOT create, infer, or silently auto-approve an approval artifact.

#### Scenario: Plan requires approval
- **WHEN** Planner returns a plan whose required tasks are not covered by a valid approval artifact
- **THEN** the Launcher stops with `status` equal to `awaiting_approval`, includes the plan and required-task identifiers, performs no Orchestrator task execution, and does not treat the pause as a scientific failure

#### Scenario: Resume receives an approval artifact
- **WHEN** `resume` is called with an approval artifact
- **THEN** the artifact is validated by the existing approval and Orchestrator contracts against the immutable plan before any task becomes executable

#### Scenario: Approval does not match the plan
- **WHEN** the supplied approval has the wrong plan identity, content binding, task scope, or budget
- **THEN** the Launcher records a structured approval failure, exits non-zero, and executes no newly approved task

### Requirement: Diagnostic report is not workflow authority
The Launcher SHALL persist an atomically updated, versioned diagnostic report containing only launcher observations and references. The report MAY contain call timestamps, opaque input/output identifiers, formal trace identifiers, the last completed call boundary, structured failure details, formal Evidence/Artifact references, and the last observed formal status. It MUST NOT contain or control formal task, run, transaction, candidate, scheduler, or workflow state.

#### Scenario: Report is deleted or edited
- **WHEN** the diagnostic report is missing, stale, or inconsistent with formal state
- **THEN** no formal state changes and the Launcher refuses any continuation that cannot be reconstructed and verified from formal authorities

#### Scenario: Formal status conflicts with the report
- **WHEN** Store, Evidence, Transaction, Planner, or Orchestrator data disagrees with the last diagnostic observation
- **THEN** the formal data wins, the discrepancy is recorded, and the report is not used to force a transition

#### Scenario: Report shape is reviewed
- **WHEN** a diagnostic report is inspected
- **THEN** it includes `launcher_run_id`, `project_id`, approved-content binding, current or failed boundary, available `prediction_invocation_id`, `prediction_run_id`, formal `workflow_id`, formal Orchestrator `run_id`, `plan_id`, `task_id`, `attempt_id`, and `transaction_id`, structured error `code`, `component`, and `message`, relevant Evidence and Artifact IDs, and the last observed formal status

#### Scenario: Prediction identity exists before Orchestrator
- **WHEN** Prediction has started or completed but Orchestrator has not been initialized
- **THEN** diagnostics may contain `prediction_invocation_id` and `prediction_run_id`, while the formal trace `run_id` remains absent

#### Scenario: Orchestrator run is initialized
- **WHEN** Orchestrator initializes the approved plan
- **THEN** diagnostics record its formal `run_id` inside formal trace fields without replacing or aliasing the distinct Prediction identities

### Requirement: Prediction identity is crash-reconstructable before execution
Before invoking Prediction, the Launcher SHALL deterministically reconstruct `prediction_invocation_id` and `prediction_run_id` from the already durable `launcher_run_id` using fixed, non-hash namespace mappings. It SHALL resolve the original exact internal Prediction run locator, including the resolved run root and `prediction_run_id`, and pass it to a Prediction-owned pre-invocation contract. That contract SHALL durably persist a Store-backed `prediction_invocation_started` receipt binding the exact locator, both Prediction identities, `launcher_run_id`, project/approved-content identity, Prediction configuration, and Design candidate inputs before any Prediction or run-directory side effect. Diagnostic metadata MAY mirror the locator but SHALL NOT authorize invocation or recovery. The Prediction invocation contract SHALL persist both identities in its existing formal manifest/handoff/Evidence contracts. `prediction_invocation_id`, `prediction_run_id`, and `formal_trace.run_id` SHALL be pairwise distinct and satisfy `prediction_invocation_id != prediction_run_id != formal_trace.run_id`; `formal_trace.run_id` SHALL remain absent until Orchestrator initialization.

#### Scenario: Prediction identities are reconstructed before invocation
- **WHEN** an approved launcher run reaches the Prediction boundary
- **THEN** the Launcher derives both Prediction identities, resolves the exact run locator, and the Prediction-owned contract durably persists the bound start receipt before it performs any Prediction side effect

#### Scenario: Prediction locator persistence fails
- **WHEN** the Prediction-owned Store-backed start receipt containing the exact resolved run locator cannot be durably persisted before the first Prediction side effect
- **THEN** the Launcher exits non-zero and does not call Prediction

#### Scenario: Prediction environment changes before resume
- **WHEN** Prediction recovery runs after `CYCPEP_PREDICTION_ROOT`, `NP_DATA`, or another ambient path selector has changed
- **THEN** resume obtains the original exact run locator from the Prediction-owned start receipt and never treats the same `prediction_run_id` under a different root as `not_started`

#### Scenario: Prediction formal completion survives launcher bookkeeping crash
- **WHEN** Prediction has formally persisted its bound run manifest, input records, handoff, and completion Evidence but the following launcher diagnostic update fails or the process crashes
- **THEN** `resume --launcher-run <id>` reconstructs the same identities, obtains and validates the original exact run locator from the Prediction-owned start receipt, uses the Prediction-owned formal validator to prove completion and all input bindings, does not call Prediction again, repairs the diagnostic observation, and continues from Critic

#### Scenario: Prediction diagnostic locator is stale or edited
- **WHEN** diagnostic locator metadata is missing or disagrees with the valid Prediction-owned start receipt
- **THEN** the receipt controls which exact run is inspected, the diagnostic may be repaired, and the diagnostic value never authorizes Prediction execution

#### Scenario: Prediction recovery is ambiguous
- **WHEN** Prediction formal records are partial, conflicting, bound to different project/approved-content/config/candidate inputs, or cannot uniquely prove the reconstructed Prediction run completed
- **THEN** resume returns a stable structured blocker with a non-zero exit code, calls neither Prediction nor Critic, and does not use directory scanning, `State.phase`, stdout parsing, or journal completion claims

#### Scenario: Prediction and Orchestrator identity namespaces remain isolated
- **WHEN** Prediction completes before Orchestrator initialization
- **THEN** `prediction_invocation_id` and `prediction_run_id` are distinct non-null values, `formal_trace.run_id` is null, and neither Prediction identity is written or aliased into the Orchestrator namespace

### Requirement: Fail-fast failure semantics
Every boundary failure SHALL stop further calls, produce a non-zero exit code, preserve already committed formal data, and write the best available diagnostic report. The Launcher MUST NOT catch an error and continue into a later stage or fabricate later-stage status.

#### Scenario: Research failure
- **WHEN** Research raises or returns a failure
- **THEN** Design and every later boundary are not called, committed Research data remains untouched, and the report identifies `research` as the failed component

#### Scenario: Design failure
- **WHEN** initial Design fails
- **THEN** Prediction and every later boundary are not called, already formal candidates or artifacts remain untouched, and the report identifies `design` as the failed component

#### Scenario: Prediction failure
- **WHEN** Prediction fails
- **THEN** Critic and every later boundary are not called, committed Prediction data remains untouched, and the report identifies `prediction` as the failed component

#### Scenario: Critic failure
- **WHEN** Critic fails
- **THEN** Planner and every later boundary are not called, committed Critic data remains untouched, and the report identifies `critic` as the failed component

#### Scenario: Planner failure
- **WHEN** Planner fails
- **THEN** no approval is inferred and Orchestrator is not initialized, while the report identifies `planner` as the failed component

#### Scenario: Worker task failure
- **WHEN** a Worker task fails through the formal execution path
- **THEN** the Launcher reports the task, attempt, transaction, structured worker error, and authoritative Orchestrator status without executing a later non-ready task

### Requirement: Resume is driven by formal state
The system SHALL provide `python -m workflow resume --launcher-run <id> [--approval <artifact>]`. Resume SHALL use the diagnostic report only to locate candidate formal records, then revalidate progress through public Store, Evidence, Agent artifact, Transaction recovery, and Orchestrator status contracts before choosing a boundary.

#### Scenario: Launcher bookkeeping fails after a committed transaction
- **WHEN** a transaction is formally `COMMITTED` and its Orchestrator task/run closure is formally visible, but the subsequent launcher report update failed
- **THEN** resume recognizes the formal completion, does not rerun that scientific action, repairs the diagnostic observation, and continues from the next legal boundary

#### Scenario: Transaction recovery is unresolved
- **WHEN** formal transaction recovery reports an unresolved commit, compensation, owner-liveness, or marker state
- **THEN** resume returns a structured blocker, exits non-zero, and does not run or retry a GPU or scientific action

#### Scenario: Running or pending run has a live transaction owner
- **WHEN** resume observes an Orchestrator run as `running` or `pending` and formal transaction inspection proves the relevant unresolved work has a live active owner
- **THEN** Launcher returns the current formal running outcome without invoking mutating recovery, compensation, task claim, Worker drain, or scientific execution

#### Scenario: Running or pending run has stale unresolved recovery
- **WHEN** resume observes an Orchestrator run as `running` or `pending` and formal transaction inspection proves unresolved work has no live owner
- **THEN** Launcher invokes the existing formal `recover_transactions` owner contract, re-reads Orchestrator after recovery, and only then returns, drains a newly ready run, or reports a structured blocker

#### Scenario: Recovery succeeds before continuation
- **WHEN** formal recovery resolves stale transaction work during resume
- **THEN** Launcher re-inspects the Orchestrator and transaction owner state before any Worker action and does not duplicate an existing claim or scientific action

#### Scenario: Repeated resume after a terminal outcome
- **WHEN** resume is called repeatedly after the formal workflow is completed, blocked, failed, or awaiting approval with no new approval
- **THEN** every call returns the same formal outcome without duplicating committed scientific work or formal artifacts

#### Scenario: Pre-Orchestrator boundary is ambiguous
- **WHEN** formal State, Evidence, Candidate, or Agent artifact records cannot uniquely prove whether a pre-Orchestrator scientific boundary completed
- **THEN** resume fails closed with a stable blocker code and does not use `State.phase`, directory scans, stdout, or journal claims to guess

#### Scenario: Project identity changes before resume
- **WHEN** the project loaded during resume has a different `project_id` from the initial durable launcher binding
- **THEN** resume returns a structured project-binding blocker, exits non-zero, and invokes no scientific or execution action

#### Scenario: Approved project content changes before resume
- **WHEN** the `project_id` is unchanged but the current approved-content identity differs from the initial durable launcher binding
- **THEN** resume returns a structured approved-content-changed blocker, exits non-zero, and does not reuse or rerun prior scientific work automatically

### Requirement: Formal outcome mapping
Launcher outcomes SHALL be projections of existing formal contracts. Orchestrator terminal and pause statuses SHALL be returned without reinterpretation as `completed`, `blocked`, `failed`, or `awaiting_approval`; pre-Orchestrator failures and blockers SHALL be explicitly attributed to their owning component.

#### Scenario: Worker drain reaches a formal pause or terminal state
- **WHEN** Worker drain returns because no task is ready
- **THEN** the Launcher reads Orchestrator status and returns its formal `completed`, `completed_required`, `blocked`, `failed`, `awaiting_approval`, or other non-ready status with task-status counts

### Requirement: Status is read-only and sanitized
The system SHALL provide `python -m workflow status --launcher-run <id>`. Status SHALL perform no scientific work or formal state transition and SHALL emit a browser-facing JSON projection that excludes secrets, internal sensitive paths, full stdout/stderr dumps, and raw exception traces.

#### Scenario: Status is queried
- **WHEN** an operator queries a known launcher run
- **THEN** the command revalidates referenced formal state, returns the current diagnostic and formal identifiers, and performs no workflow action

#### Scenario: Error text contains sensitive or verbose process output
- **WHEN** a stage failure includes credentials, sensitive local paths, multiline stdout/stderr, or a traceback
- **THEN** the browser-facing result contains only a sanitized bounded message and structured code/component while the Launcher stores no full stdout dump

### Requirement: Compatibility and deletion safety
Existing Agent CLIs, public Python seams, formal artifacts, and selfcheck behavior SHALL remain compatible. Deleting launcher diagnostics SHALL NOT delete, roll back, or mutate formal project, candidate, evidence, plan, run, task, artifact, or transaction data.

#### Scenario: Legacy entry point runs without launcher metadata
- **WHEN** an existing caller invokes a public Agent seam without the new optional correlation inputs
- **THEN** its prior behavior and artifact contracts remain unchanged

#### Scenario: Legacy Prediction manifest shape is unchanged
- **WHEN** Prediction runs or resumes without Launcher correlation metadata
- **THEN** its expected and persisted run manifest completely omit Launcher-only correlation keys rather than adding keys with `null` values, preserving existing strict manifest equality and `--resume` behavior

#### Scenario: Diagnostic cleanup
- **WHEN** an operator removes a launcher diagnostic report
- **THEN** all formal workflow data remains available through its existing authorities

### Requirement: Pre-Planner recovery follows causal authority order
The Launcher SHALL validate Prediction before inspecting or invoking Critic or Planner. Prediction ambiguity, conflict, partial completion, or absence SHALL take precedence over any stale downstream Critic or Planner record. Critic SHALL be inspected or invoked only after current-run Prediction completion is formally validated, and Planner SHALL be inspected or invoked only after current-run Critic completion is formally validated.

#### Scenario: Prediction blocker overrides stale downstream completion
- **WHEN** Prediction is blocked or `started_without_completion` while Critic or Planner completion records also exist
- **THEN** the Launcher returns the Prediction-owned blocker and performs no Critic, Planner, approval, Orchestrator, or Worker continuation

#### Scenario: Prediction has not started
- **WHEN** the Prediction-owned validator returns `not_started`
- **THEN** the Launcher may recover only Research, initial Design, and Prediction, then revalidates Prediction before observing Critic

### Requirement: Diagnostic observations preserve unresolved failure and trace
Ordinary diagnostic observations SHALL preserve `failure`, `failed_boundary`, and all previously known formal trace identifiers. A failure MAY be cleared only through an explicit operation invoked after the owning formal contract proves that the failed condition is resolved.

#### Scenario: Formal failure remains unchanged across status and resume
- **WHEN** a Worker failure and its task, attempt, and transaction identifiers are recorded and repeated status or resume calls observe the same formal state
- **THEN** the diagnostic failure and all identifiers remain present and no Worker action is repeated

#### Scenario: Formal owner proves recovery
- **WHEN** the relevant owner validator later proves the failed condition is resolved
- **THEN** Launcher may explicitly clear the diagnostic failure and continue; an unrelated observation alone never clears it

#### Scenario: Transaction blocker is formally resolved
- **WHEN** a prior diagnostic records `transaction_recovery_unresolved` and the transaction owner later proves the referenced recovery state clean
- **THEN** Launcher explicitly clears only that transaction failure before returning `ready`, `running`, `pending`, or a terminal formal outcome, so the browser-safe result contains no stale transaction error

### Requirement: Status transaction inspection is read-only
`status` SHALL call a public transaction/recovery owner contract that inspects the current recovery state without invoking mutating recovery, claiming tasks, writing markers, or transitioning transactions. Unresolved recovery SHALL produce a non-zero structured blocker with available transaction identifiers.

#### Scenario: Unresolved transaction during status
- **WHEN** read-only formal recovery inspection reports transaction `TX123` unresolved
- **THEN** `status` returns `blocked` with `transaction_recovery_unresolved` and `TX123`, and performs no formal mutation

#### Scenario: Orchestrator identifiers precede transaction inspection
- **WHEN** a stale diagnostic is missing current Orchestrator identifiers and read-only recovery inspection reports `TX123` unresolved
- **THEN** Launcher non-destructively merges the formal `workflow_id`, `run_id`, and `plan_id` before reporting the blocker, retains any existing `task_id` and `attempt_id`, and reports `transaction_id` as `TX123` without claiming that a boundary completed

### Requirement: Critic review correlation is current-run scoped
New `critic_review` Evidence SHALL include the source `prediction_run_id`. Inspection SHALL filter explicit events for the current Prediction run before opening report artifacts. Legacy events without that field SHALL remain usable only when their report source can be uniquely validated as the current run; unrelated legacy records SHALL not poison current recovery, while possibly-current but unverifiable or conflicting current records SHALL fail closed.

#### Scenario: Unrelated legacy Critic report is broken
- **WHEN** an old Prediction run has a broken legacy Critic record and the current run has one valid explicitly correlated Critic record
- **THEN** the current Critic boundary is `completed`

#### Scenario: Current Critic evidence is broken or conflicting
- **WHEN** the current run has a broken explicitly correlated report or conflicting current-run records
- **THEN** Critic recovery is ambiguous and Planner is not inspected or invoked

#### Scenario: Current Prediction has no Critic record and unrelated legacy history is broken
- **WHEN** the current Prediction run is formally completed, no Critic record belongs or might belong to it, and an older unrelated legacy Critic artifact is broken
- **THEN** Critic inspection returns `not_started` for the current run and Launcher may invoke Critic once for that run

#### Scenario: Possibly-current legacy Critic record is unverifiable
- **WHEN** a legacy Critic record might belong to the current Prediction run but its source cannot be safely confirmed
- **THEN** Critic recovery fails closed and Launcher does not invoke Critic or Planner

### Requirement: Research completion references remain project scoped
Research validation SHALL query and validate referenced Research Evidence within the expected `project_id`, agent, and event-type scope. A completion receipt SHALL NOT become valid by referencing Research Evidence owned by another project.

#### Scenario: Research receipt references another project
- **WHEN** a Project A completion receipt references Project B `research_targets` Evidence
- **THEN** Research validation returns invalid or conflicting rather than `completed`, and Design is not invoked

### Requirement: One explicit runtime path context owns all formal storage
Launcher, Research, Design, Prediction, and Store SHALL use the same officially resolved `ProjectContext` paths for data, Evidence, and database access. Launcher SHALL NOT independently parse runtime path environment variables, guess from the current directory, or silently fall back to another repository data root. Process-scoped compatibility binding SHALL restore previous runtime globals after success or failure.

#### Scenario: Custom runtime paths are supplied
- **WHEN** the approved project resolves custom data, Evidence, and database paths through the official context contract
- **THEN** every Launcher boundary and Store access uses those exact paths

#### Scenario: Runtime binding exits with an exception
- **WHEN** a bound Launcher operation raises
- **THEN** all previous process runtime bindings are restored and no second Store is selected

### Requirement: Runtime locator binding survives command boundaries
The initial diagnostic SHALL durably store the exact resolved internal data, Evidence, formal database, and approved-project locators before any formal Research or scientific side effect. Later `status` and `resume` commands SHALL reconstruct their `ProjectContext` from that durable locator binding and SHALL NOT re-resolve formal storage from current ambient path selectors. The locator binding selects where formal authorities are queried but MUST NOT declare any workflow, scientific, task, or transaction state, and browser-safe output MUST omit its internal paths.

#### Scenario: Ambient runtime paths change after launch
- **WHEN** launch writes formal receipts using runtime locator set A and a later process supplies different data, Evidence, or database environment selectors for set B
- **THEN** status and resume use the durable locator set A, never classify the boundaries as `not_started` from Store B, and never rerun Research, Design, Prediction, or Worker work in B

#### Scenario: Durable runtime locator cannot be restored
- **WHEN** the original runtime locator binding is missing, invalid, conflicting with the approved project locator, or cannot safely open its formal Store
- **THEN** status or resume returns a structured recovery blocker and does not fall back to ambient paths, repository defaults, or a new database

#### Scenario: Runtime locator persistence fails before science
- **WHEN** the initial diagnostic cannot durably persist the complete runtime locator binding
- **THEN** launch exits non-zero before Research or any other formal or scientific side effect

### Requirement: Latest Planner plan contract is preserved
Launcher SHALL pass through and validate the current immutable Planner plan without deleting, reconstructing, or downgrading `decision_metadata`, compute estimates, budget metadata, plan identity, or approval-bound fields. Approval validation and Orchestrator initialization SHALL receive the same current plan contract produced by Planner.

#### Scenario: Planner emits compute-aware metadata
- **WHEN** Planner produces an immutable plan containing `decision_metadata`, compute estimates, and budget status or limits
- **THEN** Launcher inspection, approval binding, resume, and Orchestrator initialization preserve and validate those fields without projecting an older plan schema
