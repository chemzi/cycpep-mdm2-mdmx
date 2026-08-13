## Purpose

Defines how Planner deterministically materializes a validated frozen ExplorationDecision into the peptide lengths of existing iterate_design jobs without ambient Evidence access.

## ADDED Requirements

### Requirement: Legacy length policy remains deterministic without a decision
Planner SHALL use each target's explicit approved `design.lengths` unchanged when no frozen ExplorationDecision is present, and SHALL use the deterministic `[8, 10, 12]` fallback only when that target has no explicit lengths.

#### Scenario: Explicit target lengths remain unchanged
- **WHEN** Planner materializes an iterate_design job without a frozen decision and the target has explicit approved lengths
- **THEN** the job uses exactly the normalized explicit length set

#### Scenario: Missing target lengths use the fixed fallback
- **WHEN** Planner materializes an iterate_design job without a frozen decision and the target has no explicit lengths
- **THEN** the job uses `[8, 10, 12]`

### Requirement: Frozen decisions only preserve or narrow the approved envelope
Planner SHALL consume the already-validated canonical dict at `state["_frozen_exploration_decision"]`. A `no_adjustment` decision SHALL preserve every target's approved length set, while an `adjustment` decision SHALL use the canonical length set from `adjustment.proposed_policy_weights` and its equivalent `preferred_lengths`, and that set MUST only narrow the approved envelope.

#### Scenario: No-adjustment preserves approved lengths
- **WHEN** a target-matching frozen decision has `decision_status=no_adjustment`
- **THEN** the materialized job retains that target's approved lengths

#### Scenario: Adjustment narrows approved lengths
- **WHEN** a target-matching frozen decision has `decision_status=adjustment` and proposes canonical length `[12]` within approved `[8, 10, 12]`
- **THEN** the materialized job uses `[12]`

#### Scenario: Proposed length is outside the approved envelope
- **WHEN** an adjustment proposes any length not approved for a materialized target
- **THEN** Planner fails closed with a Planner contract error and emits no jobs

#### Scenario: Decision target scope differs from materialized targets
- **WHEN** the frozen decision's canonical target set differs from the required target set
- **THEN** Planner fails closed with a Planner contract error and emits no jobs

### Requirement: Decision materialization does not alter job policy
Applying a frozen decision SHALL change only job peptide lengths. Planner SHALL preserve target allocation, per-target proposal counts, route selection, deterministic seed derivation, protocol and threshold inputs, and the approved project configuration.

#### Scenario: Adjustment preserves non-length job fields
- **WHEN** the same State, budgets, requested proposal count, and seed material are materialized with a valid narrowing adjustment
- **THEN** proposal counts, routes, target allocation, and seeds equal those produced without the adjustment

#### Scenario: Repeated materialization is identical
- **WHEN** Planner materializes jobs repeatedly from identical State, frozen decision, budgets, requested proposal count, and seed material
- **THEN** the complete design job arrays are identical

### Requirement: Planner materialization is independent of ambient experience
Planner SHALL NOT read or write ambient experience, Evidence, the current time, or the ambient filesystem while materializing design jobs. The legacy experience module SHALL remain available to upstream ExplorationDecision construction.

#### Scenario: Ambient experience is inaccessible
- **WHEN** ambient experience and Evidence APIs are configured to fail if accessed during job materialization
- **THEN** Planner still produces the expected jobs without invoking those APIs

### Requirement: Existing execution contract remains compatible
Planner SHALL continue to emit the current `iterate_design` action and current `design_jobs` item fields without adding a new Action, schema, Worker, transaction, or Design executor behavior.

#### Scenario: Existing job shape is retained
- **WHEN** Planner materializes any valid design job
- **THEN** each job contains the existing route, target_id, lengths, proposal_count, and seed fields

