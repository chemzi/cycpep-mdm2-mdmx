## Purpose

Defines the formal, approval-gated bootstrap execution that materializes and ingests initial Prediction evidence after committed Initial Design and before Critic, using the existing Planner, Orchestrator, ExecutionWorker, transaction, and Prediction owner contracts.

## ADDED Requirements

### Requirement: Bootstrap eligibility derives from formal Initial Design state
For a new Launcher production run, the system SHALL create or recover an initial Prediction bootstrap plan only when Research is complete, Initial Design has one valid completion bound to a `COMMITTED` transaction, the authoritative candidate registrations in that transaction exactly match the Design completion candidate set, and no current Critic-ready Prediction completion exists. The candidate scope SHALL be the complete committed set and MUST NOT be selected, truncated, reordered into a different identity, or inferred from a projection or ambient directory.

#### Scenario: Committed Design requires bootstrap Prediction
- **WHEN** Initial Design completed with a committed authoritative candidate set and the current Launcher run has no Prediction completion
- **THEN** the system resolves one bootstrap Prediction plan whose candidate scope equals that committed set

#### Scenario: Uncommitted or contradictory Design cannot bootstrap
- **WHEN** the Design completion has no committed transaction proof or its completion and authoritative registration candidate sets differ
- **THEN** the system fails closed at Design recovery and creates no bootstrap plan

#### Scenario: Candidate scope cannot be silently reduced
- **WHEN** the committed Design set exceeds a configured or approved Prediction candidate limit
- **THEN** the system reports a budget or scope blocker and does not execute a subset

### Requirement: Planner owns one immutable bootstrap execution plan
The Planner SHALL publish a canonical immutable bootstrap plan distinguished from a Critic-driven plan by an explicit bootstrap source variant. Its source SHALL bind the approved project content, Launcher identity, Initial Design invocation and completion, committed Design transaction, and exact candidate set. The plan SHALL contain exactly one executable task using the registered `evaluate_new_design_candidates` action with the active Prediction protocol and explicit candidate scope. Bootstrap planning SHALL make no Critic-derived recommendation or scientific candidate-selection decision.

#### Scenario: Bootstrap plan is deterministic and executable
- **WHEN** the same formal Design source, project approval, Prediction protocol, Planner configuration, and candidate set are planned again
- **THEN** the same immutable plan identity and exact registered action contract are recovered without publishing a second plan

#### Scenario: Bootstrap and Critic-driven plans remain distinct
- **WHEN** a plan is sourced from Initial Design rather than an immutable Critic report
- **THEN** it is validated as the bootstrap source variant and cannot be interpreted as a Critic-driven iteration plan

#### Scenario: No second Prediction executor is introduced
- **WHEN** the bootstrap plan is inspected for executable work
- **THEN** its only scientific action is the existing registered `evaluate_new_design_candidates` action

### Requirement: Initial Prediction execution requires explicit approval
The bootstrap plan SHALL use the existing plan-bound approval contract and SHALL declare its Prediction task, exact candidate scope, resource class, candidate limit, and execution budget. Launcher SHALL return `awaiting_approval` until valid explicit approval covers that immutable plan and task. It MUST NOT create, infer, widen, or auto-approve a budget.

#### Scenario: Design completion pauses before scientific Prediction
- **WHEN** Design is complete, no Critic-ready Prediction exists, and no valid bootstrap approval is available
- **THEN** Launcher returns `awaiting_approval`, exposes the bootstrap plan and required task identity, runs no Prediction scientific tool, and writes no new direct ingest-only `prediction_invocation_started` event

#### Scenario: Approval bound to a different scope fails closed
- **WHEN** an approval references a different plan, candidate scope, task scope, protocol, or insufficient budget
- **THEN** approval validation fails and neither Orchestrator initialization nor scientific execution occurs

#### Scenario: Valid explicit approval enables orchestration
- **WHEN** approval is valid for the exact immutable bootstrap plan and required Prediction task
- **THEN** Launcher initializes the existing Orchestrator and allows ExecutionWorker to claim the task under the approved budget

### Requirement: ExecutionWorker owns scientific execution and transaction publication
Launcher SHALL NOT invoke Prediction scientific subprocesses or materialize scientific artifacts. ExecutionWorker SHALL execute the approved registered `evaluate_new_design_candidates` handler, which SHALL reuse complete compatible evidence or generate missing full evidence through its existing toolchain and SHALL stage Prediction ingest effects through the existing transaction contract. Formal Prediction Candidate, State, Artifact, Evidence, handoff, and task-output effects SHALL become authoritative only through successful transaction and task completion.

#### Scenario: Missing evidence uses the existing handler
- **WHEN** an approved bootstrap task has candidates without complete compatible Prediction artifacts
- **THEN** ExecutionWorker invokes the existing `evaluate_new_design_candidates` handler and does not invoke a second materializer implementation

#### Scenario: Tool preflight failure publishes no false completion
- **WHEN** the existing handler reports a required scientific-tool preflight failure
- **THEN** the task becomes formally failed, its Prediction transaction does not commit, no authoritative handoff completion is published, and Launcher attributes the blocker to Prediction execution

#### Scenario: Successful Worker transaction publishes formal output
- **WHEN** the handler produces complete evidence and its transaction commits
- **THEN** the task output, formal Artifacts, Prediction records, Evidence, handoff, and Candidate/State effects are bound to the same approved task attempt and committed transaction

### Requirement: Formal trace identity and Prediction identity remain distinct
The bootstrap execution SHALL preserve `run_id` as the Orchestrator run identity in formal trace. Prediction domain identity SHALL be carried only as `prediction_run_id` in Prediction-owned payloads, records, handoff, and recovery references. Payload fields MUST NOT overwrite or alias reserved trace keys.

#### Scenario: Worker Prediction identity differs from Orchestrator identity
- **WHEN** the approved bootstrap task executes Prediction
- **THEN** formal Evidence uses the Orchestrator `run_id`, Prediction payloads use `prediction_run_id`, and the two values may differ without conflict

#### Scenario: Trace conflict fails before commit
- **WHEN** a staged Prediction event carries a reserved trace value inconsistent with the Worker trace context
- **THEN** the transaction fails before commit and publishes no formal Prediction mutation

### Requirement: Scientific configuration identity is independent of execution locators
The bootstrap invocation, immutable plan, approved task, and execution receipts SHALL carry one auditable Prediction protocol/configuration identity sufficient to bind the scientific execution. That identity SHALL include the Prediction protocol version, ColabDesign source commit, Boltz version, PyRosetta version, model/checkpoint identity, and canonical configuration digest required by the active protocol. Machine-specific absolute paths SHALL be execution locator or deployment metadata only and MUST NOT participate in scientific identity, plan determinism, cache equivalence, or result identity.

#### Scenario: Same scientific configuration at a different deployment path
- **WHEN** two deployments use identical validated protocol/configuration, tool versions, source commit, model/checkpoint content identity, and approved inputs but resolve executables, repositories, caches, or output roots at different absolute paths
- **THEN** their scientific configuration identity is equal while their execution locator metadata may differ

#### Scenario: Scientific tool or model identity changes
- **WHEN** the Prediction protocol, ColabDesign commit, Boltz version, PyRosetta version, model/checkpoint identity, or canonical scientific configuration changes
- **THEN** the scientific configuration identity changes and existing approval, resume, or cache evidence cannot be reused as if execution were equivalent

#### Scenario: Locator drift does not select different science
- **WHEN** an absolute runtime path changes without a validated matching scientific identity and formal execution binding
- **THEN** execution or recovery fails closed rather than treating the new path as equivalent merely because it exists

#### Scenario: Paths remain available for operations and audit
- **WHEN** ExecutionWorker launches or recovers an approved Prediction task
- **THEN** it may record bounded machine paths as internal execution locator metadata while browser-safe or scientific identity projections omit them

#### Scenario: Later Prediction tasks use the same identity contract
- **WHEN** Planner generates a new Critic-driven task using `evaluate_new_design_candidates`
- **THEN** that task carries the same required path-independent Prediction execution identity as a bootstrap task

#### Scenario: Historical unstarted task lacks execution identity
- **WHEN** an immutable historical Prediction task without the new execution identity has not begun scientific execution after the upgraded contract is deployed
- **THEN** it remains readable audit history but fails closed for new execution and must be regenerated from its immutable source rather than rewritten or implicitly upgraded

### Requirement: Launcher recovers Prediction from formal execution proof
After bootstrap execution, Launcher SHALL resolve Prediction only from the immutable bootstrap plan, approved Orchestrator run, exact task and attempt, task output binding, committed transaction, formal Artifacts and Evidence, and Prediction owner records and handoff. It SHALL NOT infer completion from diagnostics, process logs, State phase, CSV/JSON projections, filesystem enumeration, or an ambient artifact root.

#### Scenario: Completed expensive execution is not repeated
- **WHEN** a bootstrap task has a valid completed task output, committed transaction, and owner-valid Prediction completion but Launcher crashes before advancing to Critic
- **THEN** resume recovers that completion and invokes no Prediction scientific tool again

#### Scenario: Unknown execution is not automatically retried
- **WHEN** a task attempt is claimed, running, partially staged, or otherwise lacks unambiguous terminal transaction and task proof
- **THEN** status and resume report the owning recovery blocker and do not create another task attempt or rerun science automatically

#### Scenario: Ambient artifact changes are ignored
- **WHEN** the configured artifact directory changes after bootstrap execution starts
- **THEN** status and resume use only the original plan/task/transaction-bound artifacts and do not scan or select the ambient directory

### Requirement: Prediction owner readiness gates Critic
Launcher SHALL invoke Critic only after the Prediction owner validator confirms that every candidate in the exact bootstrap scope has one authoritative record in a Critic-ready owner status. This capability MUST NOT redefine L1-L7, thresholds, status computation, or `CRITIC_READY_STATUSES`.

#### Scenario: Complete Prediction advances to Critic
- **WHEN** the approved Worker execution completes and every authoritative candidate record passes the existing owner readiness contract
- **THEN** Launcher marks Prediction completed and may invoke Critic with the formally bound handoff

#### Scenario: One pending candidate blocks Critic
- **WHEN** any candidate in the bootstrap scope recomputes to `prediction_pending`
- **THEN** Launcher reports `prediction_execution_incomplete` and does not inspect or invoke Critic

#### Scenario: Integrity contradiction retains its specific blocker
- **WHEN** task output, transaction, Artifact, Evidence, handoff, record, candidate scope, project, protocol, or run binding contradicts another formal source
- **THEN** Launcher reports the existing specific correlation, integrity, transaction, or recovery blocker rather than treating the state as scientific incompletion

### Requirement: Command recovery is consistent and legacy runs remain immutable
For the same formal state, `launch`, read-only `status`, and `resume` SHALL return consistent bootstrap plan, approval, execution, Prediction readiness, and downstream attribution. Existing Launcher runs that already contain direct Prediction invocation evidence SHALL remain under their existing recovery contract and MUST NOT be converted, backfilled, or restarted through the bootstrap path.

#### Scenario: Commands agree at approval pause
- **WHEN** the current formal state is a bootstrap plan awaiting approval
- **THEN** launch, status, and resume all report `awaiting_approval` for the same plan and required task without scientific execution

#### Scenario: Commands agree on failed Worker execution
- **WHEN** the approved bootstrap task has a formal terminal failure
- **THEN** launch, status, and resume report that same task/Prediction execution failure and do not fall back to direct ingest

#### Scenario: Old failed Launcher run is not modified
- **WHEN** an existing run has an immutable direct Prediction start/completion and `prediction_execution_incomplete`
- **THEN** the new code preserves and reports that run's existing blocker and creates no bootstrap plan, approval, Orchestrator run, task, transaction, Artifact, or replacement handoff for it

### Requirement: Terminal bootstrap failure supports only operator-explicit reapproval
A formally failed bootstrap Prediction plan, Orchestrator run, task attempt, and transaction SHALL remain immutable. Plain launch, status, and resume MUST continue reporting that terminal failure and MUST NOT retry it. After the deployment or runtime is repaired, an operator MAY explicitly request a new bootstrap Prediction execution. The system SHALL then create one new immutable retry plan sourced from the same Initial Design completion, same committed Design transaction, and exact same candidate set, bind the prior failed execution, and require a new explicit approval before a new Orchestrator run or Worker task is created. Research and Initial Design SHALL NOT be rerun for this retry.

#### Scenario: Plain resume does not retry terminal scientific failure
- **WHEN** the current bootstrap Prediction task is formally failed and the operator calls status or ordinary resume
- **THEN** the same failure is returned and no plan, approval, Orchestrator run, task attempt, transaction, or scientific process is created

#### Scenario: Explicit retry creates a newly approvable immutable plan
- **WHEN** the current bootstrap Prediction execution is formally failed, its transaction is terminal and unambiguous, no Critic-ready Prediction completion exists, and the operator explicitly requests retry
- **THEN** Planner publishes one new retry plan bound to the prior failure and the unchanged Initial Design completion, committed transaction, and exact candidate set, and Launcher returns `awaiting_approval`

#### Scenario: Retry cannot change Design candidate authority
- **WHEN** a requested retry changes, removes, adds, or reorders into a different identity any candidate from the original committed Design set
- **THEN** retry planning fails closed and creates no new execution authority

#### Scenario: Retry requires a new plan-bound approval
- **WHEN** an operator supplies approval from the failed plan or an approval that does not bind the new retry plan, execution identity, task, exact candidate set, and budget
- **THEN** approval validation fails and no retry Orchestrator run starts

#### Scenario: Approved retry reuses Design without rerunning it
- **WHEN** the new retry plan receives valid explicit approval
- **THEN** the existing Orchestrator/ExecutionWorker path executes `evaluate_new_design_candidates` for the exact preserved candidate set without invoking Research or Initial Design

#### Scenario: Ambiguous or active execution cannot be retried
- **WHEN** the prior execution is running, claimed, partially staged, unresolved, or lacks unambiguous formal terminal failure and transaction proof
- **THEN** the retry request fails closed and does not create a new plan or task attempt
