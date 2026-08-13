## Purpose

Defines the formal identity evidence required to prove that one committed Prediction transaction belongs to the exact approved execution before Launcher consumes its publication.

## ADDED Requirements

### Requirement: Transactional Prediction evidence shares one domain run identity
Every authoritative `prediction_recorded`, `battery_evaluated`, and `prediction_handoff_ready` Evidence event required for a committed Prediction publication SHALL carry the same non-empty `prediction_run_id` as the committed handoff document, while retaining the exact approved workflow, Orchestrator run, plan, task, attempt, transaction, and candidate bindings owned by the execution trace.

#### Scenario: Real transactional writer produces a publishable evidence set
- **WHEN** the registered Prediction action writes record, battery, and handoff Evidence through the transaction-effect writer and Worker formalization seam
- **THEN** every required event carries the handoff's Prediction run identity and the existing Launcher publication validator accepts the committed exact-scope publication

#### Scenario: Any required event omits or changes the Prediction run identity
- **WHEN** a committed required record, battery, or handoff Evidence event has an absent or different `prediction_run_id`
- **THEN** Launcher SHALL fail closed with `prediction_execution_correlation_invalid`

### Requirement: Publication identity repair preserves transaction authority
The repair SHALL populate the existing Prediction identity at the Prediction-owned transaction writer and SHALL NOT infer, synthesize, or repair identity in Launcher, Store readers, or historical Evidence.

#### Scenario: Existing failed invocation remains immutable
- **WHEN** the fix is deployed after an invocation was blocked by incomplete Prediction Evidence correlation
- **THEN** the blocked invocation and its committed Evidence remain unchanged and validation occurs only through a fresh invocation
