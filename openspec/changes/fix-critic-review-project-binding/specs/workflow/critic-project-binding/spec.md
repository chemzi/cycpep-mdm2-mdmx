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

### Requirement: Unbound legacy Evidence does not suppress current bound persistence
An existing `critic_review` event without the current project binding MUST remain immutable and non-authoritative, and MUST NOT prevent a supported Critic rerun from appending one current project-bound event for the same deterministic report.

#### Scenario: Resume encounters the preserved unbound E2E event
- **WHEN** supported continuation reruns Critic for a deterministic report whose prior event has the same report ID but no project binding
- **THEN** the old event remains unchanged and one new project-bound `critic_review` event is persisted

#### Scenario: Current bound event is already present
- **WHEN** Critic persistence finds an existing event with the same project, prediction run, report ID, and report digest
- **THEN** it remains idempotent and does not append a duplicate current event
