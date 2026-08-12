## Purpose

Ensure Critic completion Evidence is explicitly bound to its owning project so project-scoped Launcher inspection and recovery can rely on exactly the report produced for that project.

## ADDED Requirements

### Requirement: New Critic completion Evidence carries the owning project
The system SHALL copy the Critic report source `project_id` into every newly persisted `critic_review` event, together with the existing prediction-run and report identity bindings, for both direct and transaction-managed persistence paths.

#### Scenario: Direct Critic persistence publishes a project-bound event
- **WHEN** Critic directly persists a report whose source identifies project P and prediction run R
- **THEN** the resulting `critic_review` Evidence identifies project P, prediction run R, and the persisted report identity

#### Scenario: Transaction-managed Critic persistence publishes the same binding
- **WHEN** an Execution transaction stages Critic effects for a report whose source identifies project P
- **THEN** the staged `critic_review` event identifies project P without changing transaction ownership or commit semantics

### Requirement: Missing Critic project identity fails closed
The Critic persistence contract MUST reject a new report effect whose source lacks a valid project identity rather than publishing project-ambiguous completion Evidence.

#### Scenario: Report source omits project identity
- **WHEN** a caller asks the Critic persistence contract to build effects for a report without `source.project_id`
- **THEN** no authoritative `critic_review` effect is produced and the caller receives a deterministic contract error

### Requirement: Launcher proof remains project scoped
The system SHALL treat a Critic report as completed for a Launcher project only when exactly one valid `critic_review` event binds that project, the expected prediction run, and the immutable report document.

#### Scenario: Matching bound report is provable
- **WHEN** one valid `critic_review` event binds project P, prediction run R, and its report document
- **THEN** project P's formal inspector reports Critic completed and exposes the report reference

#### Scenario: Cross-project event is not authoritative
- **WHEN** a valid Critic event binds project Q while project P is being inspected
- **THEN** the event does not prove Critic completion for project P

#### Scenario: Report source disagrees with inspected project
- **WHEN** a Critic event identifies inspected project P but its immutable report source identifies project Q
- **THEN** the inspector fails closed even when prediction-run, report ID, and report digest bindings otherwise match

### Requirement: Unbound legacy Evidence does not suppress current bound persistence
An existing `critic_review` event without the current project binding MUST remain immutable and non-authoritative, and MUST NOT prevent a supported Critic rerun from appending one current project-bound event for the same deterministic report.

#### Scenario: Resume encounters the preserved unbound E2E event
- **WHEN** supported continuation reruns Critic for a deterministic report whose prior event has the same report ID but no project binding
- **THEN** the old event remains unchanged and one new project-bound `critic_review` event is persisted

#### Scenario: Current bound event is already present
- **WHEN** Critic persistence finds an existing event with the same project, prediction run, report ID, and report digest
- **THEN** it remains idempotent and does not append a duplicate current event

### Requirement: Transactional Evidence trace conflicts fail before commit
The Execution Worker MUST reject a staged Evidence event whose supplied trace key conflicts with the Worker TraceContext, using the formal Evidence event conflict contract for every `TRACE_KEYS` field before transaction commit.

#### Scenario: Critic project conflicts with Worker trace
- **WHEN** a transaction stages a Critic event for project P while the Worker TraceContext identifies project Q
- **THEN** the transaction does not commit, no `critic_review` event is stored, and no formal State mutation is visible

### Requirement: Transactional Prediction identity remains outside formal trace
Transactional Prediction Evidence MUST express the Prediction domain run identity as `prediction_run_id`. Its top-level `run_id` MUST be supplied only by the Worker TraceContext as the distinct Orchestrator run identity.

#### Scenario: Prediction and Orchestrator runs remain distinct
- **WHEN** a Prediction transaction emits formal Evidence for Prediction run P under Orchestrator run O
- **THEN** committed Evidence carries `prediction_run_id` P and top-level `run_id` O, P may differ from O, and formal Evidence validation succeeds

#### Scenario: Launcher Prediction correlation remains unchanged
- **WHEN** transactional Prediction Evidence includes existing Launcher correlation fields
- **THEN** their values and the existing Launcher Prediction completion decision remain unchanged by the field normalization

### Requirement: Critic project identity uses the formal Trace ID contract
The shared Critic persistence effect MUST validate immutable `source.project_id` with the repository's formal Trace ID contract before returning any State, history, or Evidence effects.

#### Scenario: Report source contains an invalid Trace ID
- **WHEN** a caller requests persistence effects for a report whose non-empty `source.project_id` violates the formal Trace ID contract
- **THEN** effect construction fails deterministically before any persistence effect is returned

### Requirement: New Critic writers cannot emit unbound Evidence
Every supported writer of a new `critic_review` event MUST require a valid project binding; legacy unbound rows remain readable but no writer may create another unbound row.

#### Scenario: Legacy convenience writer omits project identity
- **WHEN** a caller invokes the Critic Evidence writer without a valid project ID
- **THEN** the writer rejects the call before appending Evidence

#### Scenario: Legacy convenience writer receives a valid project identity
- **WHEN** a caller invokes the Critic Evidence writer with a valid project ID
- **THEN** the newly appended `critic_review` event includes that project binding
