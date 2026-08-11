## Purpose

Defines the Design and Prediction owner evidence required for Launcher to advance without turning scientific-tool failure, empty output, or scientifically incomplete Prediction records into false completion.

## ADDED Requirements

### Requirement: Initial Design completion requires usable candidates
Launcher SHALL treat an initial Design invocation as completed only when exactly one correlated completion receipt references a non-empty set of existing candidates and all of its formal references are valid. The Design owner MUST distinguish a normal, successfully executed zero-result from a classified scientific-tool failure before writing a terminal receipt. A normal zero-result SHALL use `initial_design_no_valid_candidates`; a tool failure SHALL use `initial_design_scientific_tool_failed` and MUST NOT be represented as a zero-result. Launcher SHALL NOT invoke Prediction for either blocker.

#### Scenario: Non-empty Design result advances
- **WHEN** the correlated initial Design invocation has one valid start and one valid completion receipt referencing at least one existing candidate
- **THEN** Launcher treats Design as completed and supplies exactly those candidate identifiers to Prediction

#### Scenario: Zero-result Design is terminally blocked
- **WHEN** every required scientific tool call finishes without a tool-execution failure and the supported initial Design job produces no valid candidate
- **THEN** Design records one correlated `design_initial_failure`, records no `design_initial_completion`, and Launcher reports `initial_design_no_valid_candidates` without invoking Prediction

#### Scenario: Scientific-tool failure is not a zero-result
- **WHEN** RFdiffusion, LigandMPNN, or refold reports a classified execution failure on the Launcher initial path
- **THEN** Design records one correlated `design_initial_failure` with `initial_design_scientific_tool_failed`, records no completion, and does not report `initial_design_no_valid_candidates`

#### Scenario: Unknown interruption remains ambiguous
- **WHEN** Design starts but exits through an unclassified exception before a terminal receipt is durable
- **THEN** later inspection reports the existing recovery ambiguity and does not infer either a normal zero-result or a tool failure

#### Scenario: Empty legacy completion fails closed
- **WHEN** a pre-change completion receipt contains an empty candidate list
- **THEN** the Design boundary rejects it as ambiguous formal state and Launcher does not advance or rewrite the record

#### Scenario: Conflicting Design terminal receipts fail closed
- **WHEN** a correlated invocation contains both a failure and a completion receipt, or contains multiple terminal receipts
- **THEN** the Design boundary reports `design_recovery_ambiguous` and no scientific route is retried

### Requirement: Prediction completion requires terminal scientific evidence
For Launcher production invocations, Prediction SHALL report completion only through the Prediction owner's formal battery-to-status and Critic-readiness/evidence contract. Every input candidate MUST have one authoritative record bound to the same project/run and candidate set; the owner contract MUST confirm required scientific evidence is present and the resulting status is Critic-ready. Missing required evidence, `prediction_pending`, or any owner-declared non-ready terminal status SHALL expose `prediction_execution_incomplete`. Structural, correlation, or evidence-integrity contradictions SHALL retain their existing more-specific blocker. Launcher and service MUST NOT duplicate the scientific status or readiness table.

#### Scenario: Owner-ready handoff advances
- **WHEN** a correlated production handoff contains exactly the invocation candidate set and every authoritative record passes the Prediction owner's readiness/evidence contract
- **THEN** Launcher treats Prediction as completed and may invoke Critic with that handoff

#### Scenario: All candidates are pending
- **WHEN** a correlated production handoff reports every candidate as `prediction_pending`
- **THEN** Launcher reports `prediction_execution_incomplete` at Prediction and does not invoke Critic

#### Scenario: Mixed terminal and pending candidates
- **WHEN** a correlated production handoff contains at least one `prediction_pending` record alongside terminal records
- **THEN** Launcher reports `prediction_execution_incomplete` at Prediction and does not invoke Critic

#### Scenario: Non-pending but non-ready status is blocked
- **WHEN** an authoritative record has a non-pending terminal status that the Prediction owner contract does not permit as Critic input
- **THEN** Launcher reports `prediction_execution_incomplete` and does not equate that status with scientific completion

#### Scenario: Required evidence is absent despite declared status
- **WHEN** an authoritative record's persisted status claims readiness but its battery lacks required scientific evidence or recomputes to a different owner status
- **THEN** Prediction rejects the invocation as an evidence-integrity contradiction and Launcher does not invoke Critic

#### Scenario: Structurally inconsistent handoff remains ambiguous
- **WHEN** the handoff candidate set, project binding, run binding, completion receipt, or authoritative record references do not agree
- **THEN** Prediction reports its existing recovery or correlation blocker rather than `prediction_execution_incomplete`

### Requirement: Launcher commands project the same formal blocker
For a fixed persisted formal state, `launch`, read-only `status`, and `resume` SHALL return the same owning boundary and blocker code. Diagnostics MAY mirror that outcome but MUST NOT create, override, or repair formal completion authority. Read-only status MUST NOT invoke scientific work or mutate formal records.

#### Scenario: Design zero-result remains stable across commands
- **WHEN** `launch` stops on a correlated Design zero-result failure
- **THEN** subsequent `status` and `resume` report boundary `design` and code `initial_design_no_valid_candidates`, and neither reruns Design nor invokes Prediction

#### Scenario: Design tool failure remains stable across commands
- **WHEN** `launch` stops on a correlated classified Design scientific-tool failure
- **THEN** subsequent `status` and `resume` report boundary `design` and code `initial_design_scientific_tool_failed`, and neither reruns Design nor invokes Prediction

#### Scenario: Prediction incomplete remains stable across commands
- **WHEN** a valid correlated handoff contains a pending candidate
- **THEN** `launch`, `status`, and `resume` report boundary `prediction` and code `prediction_execution_incomplete` until formal Prediction evidence changes through its owner

#### Scenario: Diagnostic failure cannot mask formal completion
- **WHEN** a diagnostic write fails after an owning boundary has durably recorded valid completion
- **THEN** a later command derives the boundary result from formal evidence and does not rerun that boundary
