## Purpose

Give browser clients a truthful, read-only view of the current project and execution graph without making the browser an owner or interpreter of workflow, persistence, or transaction state.

## ADDED Requirements

### Requirement: Browser observability uses a versioned read model
The system SHALL expose a versioned read-only workbench response for the current project containing project identity, current workflow and run identity, tasks, typed actions, executions and transactions, candidates, evidence, artifacts, protocol provenance, trace identifiers, and blockers when those records exist.

The `project` view SHALL be current-project scoped. The `workflow`, `run`, `tasks`, `executions`, and `transactions` views SHALL be scoped only to the validated current workflow/run. Project-scoped `candidates`, `evidence`, and `artifacts` SHALL preserve their available formal trace linkage and SHALL distinguish current-run records from historical-run or unlinked project records. Every bounded collection SHALL report `total` as the number of matching formal records before limiting, `returned` as the number of returned items, and `truncated` as whether `returned` is less than `total`.

#### Scenario: A current run exists
- **WHEN** a browser requests the Frontend V2 workbench read model for a project with a current Orchestrator run
- **THEN** the response identifies the project, workflow, run, tasks, actions, observable executions and transactions, candidates, evidence, artifacts, protocol bindings, trace relationships, and blockers using opaque identifiers

#### Scenario: No current run exists
- **WHEN** a browser requests the workbench read model for a valid current project that has no current Orchestrator run
- **THEN** the response returns the project and available Store-backed collections with an explicit no-current-run state and does not synthesize a workflow stage

#### Scenario: Project history spans multiple runs
- **WHEN** project-scoped candidates, evidence, or artifacts include records from the current run, historical runs, or records without a formal run link
- **THEN** each record preserves its available trace identifiers and is identified as current-run, historical-run, or unlinked without being merged into current workflow/run state

#### Scenario: A bounded collection reaches its response limit
- **WHEN** a bounded workbench collection has more matching formal records than its response limit
- **THEN** `total` reports the pre-limit count, `returned` reports the item count in the response, and `truncated` is true exactly when `returned` is less than `total`

### Requirement: Workflow and task state comes from formal execution contracts
The workbench response SHALL derive run and task status from the validated Orchestrator run, task definitions from the bound Planner plan, and action capability from the canonical Action Catalog and Action Registry.

#### Scenario: Executable task is ready
- **WHEN** a task is ready in the validated run and its typed action is executable with a registered handler
- **THEN** the response reports the task status and action as executable without inferring readiness from Agent order, phase labels, or evidence counts

#### Scenario: Action is unavailable
- **WHEN** a task action is non-executable, lacks a registered handler, is blocked by its execution gate, awaits approval, or has an unsatisfied dependency
- **THEN** the response reports the canonical availability and reason codes rather than presenting the action as runnable

#### Scenario: Tasks do not form a fixed linear pipeline
- **WHEN** a plan contains dependencies, optional tasks, blocked tasks, or actions outside the historical Research → Design → Prediction → Critic sequence
- **THEN** the response preserves the plan task graph and does not coerce it into a fixed frontend state machine

### Requirement: Formal Store data remains authoritative
The read model SHALL obtain candidates, evidence, artifact metadata, and recorded transaction metadata through the formal Store seam and SHALL NOT read JSON, CSV, or JSONL projections as an independent authority.

#### Scenario: Projection disagrees with SQLite
- **WHEN** a compatibility projection differs from formal Store data
- **THEN** the browser response reflects the Store and does not merge or reverse-synchronize projection content

#### Scenario: Browser requests observability data
- **WHEN** the browser obtains the workbench response
- **THEN** it receives serialized domain views rather than database access, SQL details, table names, or raw persistence rows

### Requirement: Transaction visibility is truthful about lifecycle limits
The read model SHALL report transaction status only from formal transaction records and their contract-bound evidence, and SHALL explicitly distinguish a task with no recorded transaction from a committed, failed, rolled-back, compensation-conflict, or otherwise recorded transaction.

#### Scenario: Transaction has a formal record
- **WHEN** a task attempt has a recorded execution transaction
- **THEN** the response binds its transaction identifier and current formal status to the matching workflow, run, task, and attempt trace identifiers

#### Scenario: Claimed task has no formal transaction record yet
- **WHEN** a task is claimed or running but no transaction record is available through the Store
- **THEN** the response marks transaction visibility as not-yet-recorded and does not infer a transaction status from a staging file, process log, or elapsed time

#### Scenario: Recovery or compensation is unresolved
- **WHEN** the formal transaction status or evidence reports an unresolved recovery or compensation conflict
- **THEN** the response exposes a stable blocker code and retains the formal status without relabeling the task as successfully complete

### Requirement: Provenance and artifacts are safe browser contracts
The response SHALL expose protocol identity, trace identifiers, evidence relationships, and artifact metadata needed for provenance while treating server paths as internal and artifact identifiers as opaque.

#### Scenario: Artifact metadata contains an internal path
- **WHEN** a formal artifact record includes a server filesystem path
- **THEN** the response omits the path and exposes only opaque identity, type, provenance, integrity metadata, producer identity, and an explicitly supported content link if one exists

#### Scenario: Protocol-bound scientific output is shown
- **WHEN** a task, artifact, candidate, or evidence record carries a protocol binding
- **THEN** the response preserves its protocol name, version, and required integrity identity without substituting the currently active protocol

#### Scenario: Evidence is correlated
- **WHEN** evidence carries workflow, run, task, attempt, transaction, candidate, or artifact trace fields
- **THEN** the response preserves those identifiers so the browser can present provenance without parsing message text

#### Scenario: Exploration shortlist evidence is presented
- **WHEN** an `exploration_shortlist` evidence event is returned to the browser
- **THEN** its `k`, `n_evaluated`, `n_passed`, `shortlist`, `calibration`, `source_event_ids`, and additive `unmapped_metrics` fields are preserved so the browser can render the scientific shortlist, while payload fields from other evidence event types are not generically exposed

### Requirement: Blockers and failures are structured
The read model SHALL expose stable blocker and failure codes with display-safe summaries for unavailable actions, failed tasks, unresolved transactions, missing current runs, and read-model integrity failures.

#### Scenario: Task fails with structured error data
- **WHEN** the validated run records a task failure
- **THEN** the response includes the task failure code and safe summary and does not require the browser to parse logs to determine the failed state

#### Scenario: Internal read binding is invalid
- **WHEN** the current run cannot be validated against its bound plan or project
- **THEN** the endpoint returns HTTP 200 in the normal success envelope with trustworthy current-project and project-scoped Store data, null `workflow` and `run`, empty current-run task, execution, and transaction collections, and a structured `workflow_binding_invalid` blocker without exposing an internal path or serving unvalidated workflow data

### Requirement: Workbench observation is side-effect free
`GET /api/v2/workbench` SHALL be a read-only observation and SHALL NOT write formal state, initialize missing state, refresh a projection, register an artifact, create evidence, mutate a run or task, or alter transaction lifecycle or recovery state.

#### Scenario: A client observes the workbench
- **WHEN** a client calls `GET /api/v2/workbench`, including when no run exists or the current binding is invalid
- **THEN** database records, compatibility projections, artifact registrations, evidence, run/task state, and transaction records and lifecycle remain unchanged after the response

### Requirement: Existing interfaces remain compatible and no controls are added
The change SHALL preserve existing `/api/v1` routes and SHALL NOT add browser start, retry, cancel, direct handler invocation, project creation expansion, scheduler control, or transaction mutation behavior.

#### Scenario: Existing client uses a v1 route
- **WHEN** an existing client calls a currently supported `/api/v1` endpoint after this change
- **THEN** its method, path, response envelope, and existing behavior remain compatible

#### Scenario: Browser requests a workflow mutation
- **WHEN** a client attempts to use the new observability interface to start, retry, cancel, dispatch, or mutate a workflow
- **THEN** no such operation is available through this read-only capability
