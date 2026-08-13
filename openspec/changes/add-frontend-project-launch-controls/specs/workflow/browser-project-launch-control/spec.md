## Purpose

Provide one narrow browser control path that turns an approved target bootstrap draft into a real Workflow Launcher run and records bounded plan approvals through the existing formal contracts.

## ADDED Requirements

### Requirement: Project creation reuses the target bootstrap authority
The control API SHALL accept a target identifier and bootstrap options, return the existing reviewable project draft, and SHALL start no workflow until that exact draft passes the existing project approval contract.

#### Scenario: Target resolves to a reviewable draft
- **WHEN** an operator submits a valid target identifier
- **THEN** the system returns the resolved project and target review fields without starting Research, Design, Prediction, or GPU work

#### Scenario: Project review is incomplete
- **WHEN** the draft has unresolved blocking review issues
- **THEN** project approval and Launcher start are rejected with a structured review blocker and no workflow run is created

#### Scenario: Approved project starts
- **WHEN** an operator approves a review-ready draft and requests launch
- **THEN** the exact approved project artifact is passed to the public Workflow Launcher and the response identifies the resulting Launcher run and current formal status

### Requirement: Launcher remains the only complete-loop coordinator
The browser control path SHALL call the public Launcher launch, operator-control status, explicit approval, and resume contracts and SHALL NOT directly invoke Research, Design, Prediction, Critic, Orchestrator, Worker, or an action handler. An explicit approval call SHALL reuse the existing Planner approval contract inside the exact Launcher-bound ProjectContext; ordinary launch, status, and resume SHALL continue to infer or create no approval.

#### Scenario: A new project enters the loop
- **WHEN** the approved project is launched from the browser
- **THEN** progression, pauses, recovery, and completion follow the same formal Launcher behavior as the command-line entry point

#### Scenario: Launch or resume reaches a blocker
- **WHEN** Launcher returns a scientific, integrity, transaction, recovery, or execution blocker
- **THEN** the control API returns that structured outcome and does not fabricate progress, retry work, or skip to a later boundary

#### Scenario: Initial Design reaches the first GPU approval pause
- **WHEN** Research and Initial Design have formally completed and the E3 bootstrap Planner produces the heavy Prediction task
- **THEN** Launcher stops before heavy Prediction execution and the control view describes that exact timing without claiming the approval prevented Initial Design compute

### Requirement: Pre-Orchestrator control inspection uses formal plan authority
The operator-control status SHALL use `launcher_run_id` to restore the exact approved project and runtime locators, then validate the Planner plan through its formal Artifact/Evidence contract. Launcher diagnostics SHALL remain a non-authoritative locator and MUST NOT supply plan content or completion truth. The same bound ProjectContext and formal SQLite Store SHALL be used for approval Evidence and launched-project workbench reads.

#### Scenario: Plan awaits approval before an Orchestrator run exists
- **WHEN** Launcher has published the E3 bootstrap plan and returned `awaiting_approval` before Orchestrator initialization
- **THEN** control status returns a browser-safe plan and resource projection from the validated formal Planner source without requiring `State.orchestrator.run_path`

#### Scenario: Frontend switches from an existing project to a new launch
- **WHEN** the adapter was previously observing another project and the frontend supplies the new `launcher_run_id`
- **THEN** control and workbench reads bind the new run's ProjectContext and formal Store and return no records from the old project's adapter binding

#### Scenario: Locator and formal plan disagree
- **WHEN** diagnostic locator data is missing, changed, or cannot lead to one matching formal project and plan
- **THEN** inspection and approval fail closed with a structured binding blocker and create no approval or execution

### Requirement: Manual approval is exactly plan and budget bound
The control API SHALL project the current immutable plan's required task scope and resource request and SHALL create approval only through the existing Planner approval contract using an explicit approver, justification, and ceilings.

#### Scenario: Operator approves the current plan
- **WHEN** the operator supplies ceilings that cover the displayed required tasks and selects approve and continue
- **THEN** the browser facade first confirms the finite displayed estimate fits the GPU-minute ceiling, then one normal plan-bound approval artifact is recorded and supplied to Launcher resume before any newly approved task executes

#### Scenario: Approval does not cover the plan
- **WHEN** a supplied task scope, GPU ceiling, proposal ceiling, candidate ceiling, plan identity, or project identity does not satisfy the current approval request
- **THEN** approval fails with the existing structured contract error and Launcher executes no newly approved task

#### Scenario: Plan changes after display
- **WHEN** the immutable plan visible to the operator is no longer the plan awaiting approval at submission time
- **THEN** the request fails as stale and the frontend must refresh before another approval attempt

### Requirement: Automatic approval is explicit and limited to the first E3 GPU gate
The operator MAY opt in at launch to automatic approval only for the `initial_prediction_bootstrap` plan created after Initial Design and before heavy Prediction by supplying approver identity, justification, maximum GPU job slots, maximum GPU minutes, maximum design proposals, and maximum Prediction candidates. The system SHALL create and validate one ordinary approval artifact for that exact plan, consume the policy after success, and require explicit human approval for every later Critic-driven or retry plan. It SHALL NOT persist a cross-run policy, widen a ceiling, approve unavailable estimates, retry failed work, or approve after binding changes. GPU slots use maximum concurrency; proposal, Prediction candidate, and estimated GPU-minute values are summed across selected required GPU tasks.

#### Scenario: Current plan fits every automatic ceiling
- **WHEN** the run reaches the first `initial_prediction_bootstrap` `awaiting_approval`, every required task has a complete resource request, every required GPU estimate is available, resources fit the ceilings, and bindings are current
- **THEN** the system records an ordinary approval for the exact required tasks and resumes that same Launcher run

#### Scenario: Later plan requires human approval
- **WHEN** the same Launcher run later produces a Critic-driven plan or an explicit retry plan
- **THEN** the consumed bootstrap policy creates no approval and the run stops at its normal explicit human approval boundary

#### Scenario: Estimate is unavailable
- **WHEN** a required GPU task has no finite Planner-owned estimate after estimate normalization
- **THEN** automatic approval stops at `awaiting_approval`, reports `approval_estimate_unavailable`, and leaves manual review available

#### Scenario: Provisional Planner estimate is available
- **WHEN** a required GPU task has a finite `simple-v1` estimate whose resource request and plan budget summary agree, while benchmark calibration remains pending
- **THEN** the estimate is eligible for ceiling comparison only after the operator explicitly selected this-run automatic approval, and the UI and approval view continue to label it provisional

#### Scenario: Plan exceeds a ceiling
- **WHEN** any required resource or combined GPU-minute estimate exceeds an operator ceiling
- **THEN** no approval is recorded, no task executes, and the response identifies which ceiling blocked automatic approval

#### Scenario: Terminal execution fails
- **WHEN** an automatically approved task reaches a terminal failure
- **THEN** the policy performs no retry or replacement approval and returns the formal Launcher failure

### Requirement: Bootstrap GPU estimates are projected without reinterpretation
The `initial_prediction_bootstrap` plan SHALL expose one consistent resource interpretation: each task's resource class, GPU slots, proposal count, candidate limit, estimate status, and estimated GPU minutes, plus a budget summary whose GPU minutes/status agrees with the normalized selected-task estimates. For later plans the control API SHALL project their existing Planner fields without normalizing or auto-approving them. It MUST preserve provisional, unavailable, and not-applicable states and MUST NOT invent estimates from elapsed time or frontend constants.

#### Scenario: Planner provides an estimate
- **WHEN** the immutable plan contains an estimated GPU duration and estimator metadata
- **THEN** the task resource request and plan budget summary agree, and the same values, units, estimator version, and provisional or calibrated status are returned for display and approval comparison

#### Scenario: Planner has no auditable estimate
- **WHEN** a GPU task reports `benchmark_required` or a null estimate
- **THEN** the API reports the estimate as unavailable and neither the adapter nor frontend substitutes a number

### Requirement: Control responses are browser-safe and idempotent
Control requests SHALL return opaque identifiers, bounded structured errors, and browser-safe status fields without exposing server paths, secrets, raw subprocess output, or tracebacks. Before launch the browser SHALL generate and session-persist one syntactically valid opaque `launcher_run_id`; the additive Launcher API SHALL accept it while preserving random server generation for legacy callers. Repeating launch with that same identifier SHALL inspect or resume the existing bound run and SHALL not create a second project approval, Launcher run, plan approval, or scientific execution. No hash-derived identity or second request-state database SHALL be introduced.

#### Scenario: Browser repeats a launch submission
- **WHEN** the same launch request is retried after a response is lost
- **THEN** the system returns the original correlated Launcher run or a structured in-progress outcome without starting a duplicate run

#### Scenario: Legacy caller omits a Launcher identity
- **WHEN** an existing Python or CLI caller launches without supplying `launcher_run_id`
- **THEN** Launcher generates its run identity exactly as before and retains prior compatibility

#### Scenario: Supplied identity is bound to another project
- **WHEN** a launch repeats an existing `launcher_run_id` with a different approved project identity or content binding
- **THEN** launch fails with a structured binding conflict and neither run is mutated

#### Scenario: Internal execution error contains paths or logs
- **WHEN** a control operation fails with internal path, process output, or traceback detail
- **THEN** the browser receives only the bounded structured code, component, and safe message
