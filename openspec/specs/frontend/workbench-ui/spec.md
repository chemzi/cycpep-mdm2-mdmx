# Frontend Workbench UI Specification

## Purpose

Provide a truthful, read-only scientific workbench that presents the versioned browser observability model without reconstructing workflow state or scientific conclusions in the frontend.

## Requirements

### Requirement: The workbench consumes only the formal V2 read contract
The Frontend V2 workbench SHALL obtain workflow-facing data from `GET /api/v2/workbench`, SHALL recognize `frontend.workbench.v2`, and SHALL NOT use `/api/v1/snapshot`, `State.phase`, files, SQLite, projections, evidence counts, or log text as fallback workflow authority.

#### Scenario: A valid V2 response is loaded
- **WHEN** the endpoint returns a successful `frontend.workbench.v2` envelope
- **THEN** the workbench renders the returned project, workflow/run, bounded collections, trace, and blockers without inventing additional formal state

#### Scenario: The V2 contract is unavailable or invalid
- **WHEN** the request fails or the response does not satisfy the required V2 envelope shape
- **THEN** the workbench presents a clear failed state and does not fall back to the legacy snapshot or synthetic data

#### Scenario: A required nested domain record is malformed
- **WHEN** a successful response has the correct schema and collection envelope but a rendered project, task/action, execution, transaction, candidate, Evidence, artifact, protocol, trace, blocker, or shortlist field is missing or has the wrong type
- **THEN** the client rejects it as a controlled contract error before rendering and does not rely on an unchecked domain cast

### Requirement: The shell presents formal project and run context
The workbench shell SHALL present the current project, the validated workflow and run when available, the returned run status as the overall run status, and structured blockers using their returned codes, scopes, identifiers, and summaries.

#### Scenario: A current run is available
- **WHEN** `workflow` and `run` are non-null
- **THEN** the shell presents their opaque identifiers and returned status without mapping them onto fixed Agent phases

#### Scenario: No current run is recorded
- **WHEN** `workflow` and `run` are null and the response contains `no_current_run`
- **THEN** the shell identifies the no-current-run state while continuing to present available project-scoped Store data

#### Scenario: The current binding is invalid
- **WHEN** the endpoint returns its trustworthy partial response with `workflow_binding_invalid`
- **THEN** the shell marks workflow/run details unavailable, shows the structured integrity blocker, and does not infer replacement workflow state

#### Scenario: A collection is truncated
- **WHEN** a bounded collection reports `truncated: true`
- **THEN** the workbench displays its `returned` and `total` counts so omission is visible to the user

### Requirement: Tasks are rendered as a dynamic action graph
The workbench SHALL render the returned task collection as a dependency graph or graph-preserving task view, including each task identity, dependencies, typed action, status, action executability and handler availability, availability reason codes, approval state, execution gate, and task-scoped blockers.

#### Scenario: Tasks have non-linear dependencies
- **WHEN** tasks branch, converge, are optional, or appear outside the historical four-Agent order
- **THEN** the view preserves their `depends_on` relationships and does not reorder them into Research → Design → Prediction → Critic

#### Scenario: An action is unavailable
- **WHEN** a task reports an unavailable action, approval requirement, blocked execution gate, or unsatisfied dependency
- **THEN** the task view presents the returned availability and reason codes and does not offer an execution control

#### Scenario: No task graph is trustworthy
- **WHEN** the current run is absent or invalid and the endpoint returns an empty current-run task collection
- **THEN** the task area presents an explicit empty or unavailable state rather than a placeholder pipeline

### Requirement: Candidate workspace preserves formal provenance
The candidate workspace SHALL present candidate identity, sequence when returned, metrics, status fields, and `current_run`, `historical_run`, or `unlinked` provenance, and SHALL associate evidence and artifacts only through returned trace identifiers.

#### Scenario: A candidate is selected
- **WHEN** a user selects a returned candidate
- **THEN** the workspace shows its returned metrics and only evidence and artifacts whose formal trace linkage identifies that candidate

#### Scenario: Historical and unlinked candidates are present
- **WHEN** project-scoped candidates span current, historical, and unlinked records
- **THEN** the workspace labels their returned run relationship and does not merge historical or unlinked data into current-run status

#### Scenario: No candidates are returned
- **WHEN** the candidate collection is empty
- **THEN** the workspace presents an honest empty state without fabricated candidates, metrics, progress, or molecular structures

### Requirement: Exploration shortlist remains distinct from scientific passing
The workbench SHALL render `exploration_shortlist` evidence using its `n_passed`, `n_evaluated`, `shortlist`, `calibration`, `source_event_ids`, and `unmapped_metrics` fields. Shortlist membership SHALL NOT imply scientific passing; each shortlist item's returned `passed` value remains authoritative.

#### Scenario: No evaluated candidate passed but a shortlist exists
- **WHEN** an exploration event reports `n_passed: 0`, `n_evaluated: N`, and a non-empty shortlist
- **THEN** the UI visibly presents `0 / N passed` separately from an `Exploration shortlist` and does not style or label shortlisted items as passed

#### Scenario: A shortlist item is inspected
- **WHEN** a shortlist item contains `candidate_id`, `passed`, `desirability`, `pareto_front`, `reason`, and `top_margin_metric`
- **THEN** those values are presented without substituting frontend-computed pass, desirability, or Pareto conclusions

#### Scenario: Calibration or metric mapping is incomplete
- **WHEN** the event reports calibrated, provisional, or unavailable counts or non-empty `unmapped_metrics`
- **THEN** the UI makes those values visible and does not hide the limitations behind a generic success state

### Requirement: Evidence is presented as structured provenance
The workbench SHALL provide an Evidence timeline and detail view using returned event type, timestamp, agent, round, targets, message when present, run relationship, protocol, and trace linkage.

#### Scenario: Evidence is inspected
- **WHEN** a user opens an evidence item
- **THEN** its structured fields and available project/workflow/run/task/attempt/transaction/candidate/artifact/parent-event links are shown without requiring stdout or log parsing

#### Scenario: Evidence is unrelated to the selected candidate
- **WHEN** an evidence item has no matching candidate trace link
- **THEN** it is not presented as evidence for that candidate, while remaining available in the project-level timeline

### Requirement: Executions and transactions expose lifecycle truth
The workbench SHALL correlate execution and transaction records by their returned task and attempt identifiers and SHALL present execution status, attempt, transaction status or visibility, structured failure, rollback, and unresolved recovery blockers without inferring lifecycle state.

#### Scenario: A running attempt has no transaction record
- **WHEN** an execution reports `transaction_visibility: not_yet_recorded`
- **THEN** the UI displays “not yet recorded” as a distinct state and does not relabel it as pending, committed, or failed

#### Scenario: Execution failed
- **WHEN** an execution contains a structured error
- **THEN** the UI presents its returned code, safe message, component, and retryability without deriving the failure from logs

#### Scenario: A transaction rolled back or recovery is unresolved
- **WHEN** a transaction reports rollback or the response contains a transaction recovery blocker
- **THEN** the UI preserves the returned transaction status and prominently presents the unresolved blocker without claiming successful completion

#### Scenario: A retry advances the current execution attempt
- **WHEN** the current execution identifies a later attempt while the bounded transaction collection still contains failed, rolled-back, or committed transactions from prior attempts for the same task
- **THEN** the UI keeps the current execution correlated only to its exact attempt and separately presents every returned transaction for that task as history

### Requirement: Artifact, protocol, and trace views preserve safe identity
The workbench SHALL present artifact opaque identity, type, role, producer and input provenance, integrity metadata, protocol name/version/integrity identity, and available trace linkage without exposing or reconstructing a server filesystem path.

#### Scenario: An artifact is inspected
- **WHEN** an artifact is associated with a candidate, task, or execution through formal trace fields
- **THEN** the inspector presents the returned opaque artifact and provenance data and never displays a server path

#### Scenario: Artifact content is not explicitly linked
- **WHEN** an artifact has no supported `content_link`
- **THEN** the UI presents metadata and an unavailable structure/content state rather than constructing a URL from an artifact identifier or internal path

#### Scenario: A supported artifact content link exists
- **WHEN** an artifact includes an explicit browser-safe `content_link`
- **THEN** the structure or content viewer may use that returned link while continuing to identify the artifact by its opaque identity

#### Scenario: The selected artifact identity changes
- **WHEN** the user switches from one linked artifact to another
- **THEN** the viewer clears the prior structure before showing the new identity as loading and resets the representation control to the default applied to the new model

### Requirement: Read-only UI states remain honest and accessible
The workbench SHALL provide distinguishable loading, empty, partial/blocked, and request-failed states, SHALL preserve the last successfully loaded response during a refresh failure with a visible stale/error indication, and SHALL expose no start, retry, cancel, dispatch, project creation, SSH, scheduler, or workflow mutation control in this change.

#### Scenario: Initial request is loading
- **WHEN** no successful V2 response has loaded yet
- **THEN** the UI presents a loading state without fake progress, candidate, execution, or scientific data

#### Scenario: Refresh fails after a successful load
- **WHEN** polling or manual refresh fails after a prior successful response
- **THEN** the prior response remains visibly marked stale and the error is shown without mutating or synthesizing its domain state

#### Scenario: Automatic polling finds a request in flight
- **WHEN** an automatic refresh interval fires before the prior workbench request has completed
- **THEN** the polling tick is skipped without aborting the in-flight request or starting a competing request

#### Scenario: A selected identity leaves a bounded response
- **WHEN** refresh removes the preferred candidate, task, Evidence, or artifact identity from the returned bounded collection
- **THEN** the visible fallback becomes the current selection and the unavailable preferred identity does not reappear automatically if a later response contains it again

#### Scenario: User reviews the read-only workbench
- **WHEN** the V2 page is rendered
- **THEN** no workflow or infrastructure mutation control is offered and key status, blocker, selection, and detail interactions remain keyboard-readable and semantically labelled
