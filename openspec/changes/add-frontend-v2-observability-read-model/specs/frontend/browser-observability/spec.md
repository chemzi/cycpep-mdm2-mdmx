## Purpose

Give browser clients a truthful, read-only view of the current project and execution graph without making the browser an owner or interpreter of workflow, persistence, or transaction state.

## ADDED Requirements

### Requirement: Browser observability uses a versioned read model
The system SHALL expose a versioned read-only workbench response for the current project containing project identity, current workflow and run identity, tasks, typed actions, executions and transactions, candidates, evidence, artifacts, protocol provenance, trace identifiers, and blockers when those records exist.

#### Scenario: A current run exists
- **WHEN** a browser requests the Frontend V2 workbench read model for a project with a current Orchestrator run
- **THEN** the response identifies the project, workflow, run, tasks, actions, observable executions and transactions, candidates, evidence, artifacts, protocol bindings, trace relationships, and blockers using opaque identifiers

#### Scenario: No current run exists
- **WHEN** a browser requests the workbench read model for a valid current project that has no current Orchestrator run
- **THEN** the response returns the project and available Store-backed collections with an explicit no-current-run state and does not synthesize a workflow stage

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

### Requirement: Blockers and failures are structured
The read model SHALL expose stable blocker and failure codes with display-safe summaries for unavailable actions, failed tasks, unresolved transactions, missing current runs, and read-model integrity failures.

#### Scenario: Task fails with structured error data
- **WHEN** the validated run records a task failure
- **THEN** the response includes the task failure code and safe summary and does not require the browser to parse logs to determine the failed state

#### Scenario: Internal read binding is invalid
- **WHEN** the current run cannot be validated against its bound plan or project
- **THEN** the endpoint returns a stable error envelope without exposing an internal path or serving an unvalidated partial workflow view

### Requirement: Existing interfaces remain compatible and no controls are added
The change SHALL preserve existing `/api/v1` routes and SHALL NOT add browser start, retry, cancel, direct handler invocation, project creation expansion, scheduler control, or transaction mutation behavior.

#### Scenario: Existing client uses a v1 route
- **WHEN** an existing client calls a currently supported `/api/v1` endpoint after this change
- **THEN** its method, path, response envelope, and existing behavior remain compatible

#### Scenario: Browser requests a workflow mutation
- **WHEN** a client attempts to use the new observability interface to start, retry, cancel, dispatch, or mutate a workflow
- **THEN** no such operation is available through this read-only capability
