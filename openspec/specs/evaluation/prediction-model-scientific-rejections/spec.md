# Prediction Model Scientific Rejections Specification

## Purpose

Defines how Prediction publishes model-level cyclic-geometry rejection as complete negative scientific evidence without weakening required model coverage or turning healthy scientific execution into a task failure.

## Requirements

### Requirement: Typed Rosetta scientific rejection
Prediction SHALL convert only the existing `rosetta_cyclic_bond_open` scientific precondition failure into a typed per-model rejection bound to the target, predictor, model ID, seed, prediction PDB identity, binder identity, and observed terminal C--N distance. Runtime, deployment, version, timeout, process, and malformed-output failures SHALL remain task-fatal.

#### Scenario: Open cyclic geometry is negative evidence
- **WHEN** one declared complex prediction has terminal C--N distance above the existing protocol limit
- **THEN** Prediction records a bound `rosetta_cyclic_bond_open` rejection without running InterfaceAnalyzer for that model
- **AND** continues evaluating the remaining models

#### Scenario: Tool failure is not scientific rejection
- **WHEN** PyRosetta import, version validation, execution, timeout, or output validation fails
- **THEN** the Prediction task fails and its transaction publishes no Candidate effects

### Requirement: Exact exclusive model coverage
For every target, each declared complex prediction SHALL appear exactly once in the union of successful Rosetta outputs and typed Rosetta rejections, keyed by predictor, model ID, seed, and prediction PDB binding. Missing, duplicate, unbound, or output-and-rejection coverage SHALL invalidate the bundle before publication or reuse.

#### Scenario: Mixed model cohort is complete
- **WHEN** two models produce Rosetta outputs and one model produces a typed rejection
- **THEN** the three exclusive entries exactly cover the three declared complex predictions
- **AND** the rejection is included in artifact inventory and provenance

#### Scenario: Invalid coverage rolls back
- **WHEN** bundle validation observes missing, duplicate, unbound, or overlapping output/rejection coverage
- **THEN** the Worker rejects the bundle before Prediction ingest and commits no formal effects

### Requirement: Canonical L3 cohort and terminal negative verdict
Canonical L3 `dg`, `sc`, and `dSASA` aggregates SHALL use the same Rosetta-eligible prediction cohort. PRODIGY observations for rejected models MAY remain diagnostic evidence but SHALL NOT enter canonical L3 aggregation. Any typed rejection SHALL explicitly fail L3 and produce the existing terminal `needs_optimization` Candidate status with L3 in `failed_layers`; it SHALL NOT become missing evidence, `prediction_pending`, `invalid`, or a zero/NaN substitute.

#### Scenario: Mixed success and rejection
- **WHEN** at least one model is rejected and remaining models have complete PRODIGY and Rosetta observations
- **THEN** all canonical L3 aggregates use only the same successful model identities
- **AND** L3 fails because the rejection is present
- **AND** the Candidate status is `needs_optimization`

#### Scenario: All models rejected
- **WHEN** every declared complex prediction has a valid typed rejection
- **THEN** Prediction publishes complete negative evidence without a fabricated numeric aggregate
- **AND** L3 is failed rather than missing
- **AND** the Candidate status is `needs_optimization`

### Requirement: Atomic batch publication and immutable history
Model-level rejection evidence SHALL use the existing Candidate-scope transaction and artifact staging boundaries. A later unexpected failure SHALL roll back all formal effects, and a previously failed invocation SHALL remain immutable and SHALL NOT be automatically retried or promoted.

#### Scenario: Later unexpected failure
- **WHEN** an earlier Candidate has a complete mixed output/rejection bundle and a later Candidate encounters a task-fatal tool failure
- **THEN** neither Candidate's Prediction record, Evidence, or Candidate update is committed

#### Scenario: New protocol invocation
- **WHEN** the rejection-capable protocol is deployed
- **THEN** old or partial bundles are not resumed as compatible evidence
- **AND** a new approved invocation generates the new artifact contract
