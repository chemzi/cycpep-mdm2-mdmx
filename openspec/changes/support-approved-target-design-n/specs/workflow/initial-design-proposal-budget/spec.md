## Purpose

Defines how approved project configuration supplies a validated proposal budget to Launcher Initial Design while preserving explicit caller precedence and legacy defaults.

## ADDED Requirements

### Requirement: Initial Design resolves one proposal budget by explicit precedence
The system SHALL resolve the Initial Design proposal count from the first present non-null value in this order: explicit `design_config.n`, `target_spec.n`, the selected approved target's `design.n`, and the legacy default `100`.

#### Scenario: Approved target omits proposal budget
- **WHEN** neither the explicit design configuration, target specification, nor selected approved target provides `n`
- **THEN** the resolved Initial Design proposal count is `100`

#### Scenario: Approved target supplies proposal budget
- **WHEN** the explicit design configuration and target specification omit `n` and the selected approved target contains `design.n=3`
- **THEN** the resolved Initial Design proposal count is `3`

#### Scenario: Target specification overrides approved target
- **WHEN** the target specification contains `n=5` and the selected approved target contains `design.n=3`
- **THEN** the resolved Initial Design proposal count is `5`

#### Scenario: Explicit design configuration overrides lower-precedence sources
- **WHEN** the explicit design configuration contains `n=7`, the target specification contains `n=5`, and the selected approved target contains `design.n=3`
- **THEN** the resolved Initial Design proposal count is `7`

### Requirement: Selected proposal budget is a positive integer
The system MUST reject a selected proposal-count value unless it is an integer, is not a boolean, and is greater than or equal to one. Validation SHALL occur before Initial Design scientific execution.

#### Scenario: Zero proposal budget is rejected
- **WHEN** the highest-precedence supplied proposal-count value is `0`
- **THEN** Initial Design job materialization fails with an invalid proposal-count error before scientific execution

#### Scenario: Non-integer proposal budget is rejected
- **WHEN** the highest-precedence supplied proposal-count value is a string, boolean, fractional number, or other non-integer value
- **THEN** Initial Design job materialization fails with an invalid proposal-count error before scientific execution

#### Scenario: Shadowed lower-precedence value does not replace an explicit budget
- **WHEN** explicit `design_config.n` is a valid positive integer and a lower-precedence source also contains `n`
- **THEN** validation and resolution use the explicit value

### Requirement: Launcher records the resolved proposal budget in the existing immutable job receipt
Launcher SHALL materialize Initial Design through the shared Design configuration resolver and SHALL record the resolved proposal count in the existing job `config.n` field. Launcher MUST NOT introduce a separate proposal-budget override or receipt field.

#### Scenario: Approved proposal budget is recorded
- **WHEN** Launcher materializes an Initial Design job for a selected approved target with `design.n=3` and no higher-precedence override
- **THEN** the immutable job receipt records `config.n=3`

#### Scenario: Legacy receipt remains equivalent
- **WHEN** Launcher materializes an Initial Design job for a legacy approved target without `design.n`
- **THEN** the immutable job receipt records `config.n=100` with the existing lengths, seed, route, target, pipeline-version, and protocol-identity fields unchanged

### Requirement: Proposal budget does not alter downstream scientific or workflow contracts
The resolved proposal budget SHALL affect only the existing Initial Design job proposal count. The system MUST NOT reinterpret it as an RFdiffusion protocol parameter, LigandMPNN per-backbone sequence count, cheap-filter setting, Prediction candidate-scope rule, or bootstrap-plan change.

#### Scenario: Narrow proposal-budget behavior
- **WHEN** an approved target sets `design.n`
- **THEN** Design protocol identity, RFdiffusion scientific parameters, LigandMPNN `n_seq_per_backbone`, cheap filtering, Prediction candidate scope, and the approval-gated bootstrap contract remain unchanged
