## Purpose

Defines benchmark-backed GPU-slot wall-minute planning and admission behavior so approved Prediction work cannot consume a full scientific run before an obviously insufficient budget is rejected.

## ADDED Requirements

### Requirement: Prediction plans use a benchmark-backed GPU-slot estimate
The Planner SHALL estimate `evaluate_new_design_candidates` in GPU-slot wall minutes for the exact Candidate count, using a configuration value calibrated from a real complete production-protocol run. The estimate SHALL cover the allocation interval that actual completion accounting measures: claim through task closure.

#### Scenario: Two-Candidate production Prediction plan
- **WHEN** an initial Prediction bootstrap plan contains two committed Candidates
- **THEN** its task carries an `estimated_gpu_minutes` value no lower than the observed 20.08 GPU-slot wall minutes from the production n=2 benchmark
- **AND** the estimate status is `estimated`

#### Scenario: Other GPU action estimates remain owned by their existing policy
- **WHEN** Planner estimates a GPU action other than `evaluate_new_design_candidates`
- **THEN** its existing proposal/candidate estimate behavior remains unchanged

### Requirement: Insufficient approved GPU minutes fail before execution
The formal approval boundary SHALL reject selected GPU tasks when `max_gpu_minutes` is less than the sum of their benchmark-backed `estimated_gpu_minutes`. Rejection SHALL occur before Orchestrator initialization can make the task ready and before Worker claim, transaction creation, or scientific subprocess execution.

#### Scenario: Approval below Prediction estimate
- **WHEN** approval selects a two-Candidate Prediction task estimated above 20 GPU-slot wall minutes but grants only 2.5 GPU minutes
- **THEN** approval validation fails closed with an insufficient-GPU-minutes contract error
- **AND** no task is claimed and no scientific executor starts

#### Scenario: Approval covers Prediction estimate
- **WHEN** approval selects the task and its GPU-minute ceiling equals or exceeds the task estimate
- **THEN** budget admission succeeds and all other approval checks continue unchanged

### Requirement: Completion remains authoritative for actual usage
The Worker SHALL continue to report actual GPU-task usage as elapsed GPU-slot wall minutes from claim through closure, and Orchestrator completion SHALL continue to reject actual aggregate usage above the approved ceiling.

#### Scenario: Runtime exceeds an admitted ceiling
- **WHEN** an admitted GPU task takes longer than its approved ceiling
- **THEN** completion fails closed under the existing `gpu_minutes_exceeded` behavior
- **AND** the transaction and formal publication behavior remains unchanged

### Requirement: Scope and compatibility remain narrow
The change SHALL NOT alter Prediction readiness, scientific protocol, execution identity, retry semantics, transaction ownership, Store schema, or any existing failed invocation.

#### Scenario: Existing failed run remains immutable
- **WHEN** the new budgeting behavior is deployed
- **THEN** prior failed Prediction invocations and their approval, transaction, Evidence, and diagnostics remain unchanged
