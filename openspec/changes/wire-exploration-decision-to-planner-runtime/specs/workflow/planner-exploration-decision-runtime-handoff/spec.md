## Purpose

Define the formal Store-backed runtime handoff that supplies one authoritative ExplorationDecision to Planner when constructing a closed-loop next-round design plan.

## ADDED Requirements

### Requirement: Formal runtime resolves the Decision from its project Store
The formal workflow runtime SHALL retrieve an ExplorationDecision only from the injected project-scoped formal Store and SHALL select the unique publication bound to the current Critic Prediction run and source round. It SHALL obtain the expected workflow identity from the unique formal `prediction_handoff_ready` publication for that Critic Prediction run, not from State or a derived default. It MUST NOT infer a Decision or workflow identity from arbitrary JSON, filesystem enumeration, logs, diagnostic journals, State projections, iteration history, or ambient experience Evidence.

#### Scenario: Unique formal Decision is available
- **WHEN** the project Store contains exactly one contract-valid `exploration_decision` publication for the current Critic Prediction run and source round
- **THEN** the runtime supplies that canonical Decision through the explicit Planner service handoff

#### Scenario: Formal publications are ambiguous or invalid
- **WHEN** matching Store publications are multiple, malformed, or internally inconsistent
- **THEN** the runtime fails closed before Planner emits or persists a plan

#### Scenario: Formal Prediction workflow identity is missing or ambiguous
- **WHEN** the current Critic Prediction run has no unique formal `prediction_handoff_ready` publication with a valid workflow identity
- **THEN** the runtime fails closed instead of allowing Planner to derive a replacement workflow ID

#### Scenario: Formal workflow identity reaches Planner without shadow State
- **WHEN** a unique Prediction publication and matching Decision are resolved
- **THEN** the runtime supplies that Prediction workflow identity in the invocation-local Planner State snapshot and does not persist or project it as a second authority

### Requirement: Closed-loop next-round planning requires a Decision
The formal workflow runtime SHALL declare an ExplorationDecision required when the current validated Critic handoff contains a recommendation whose public Planner mapping produces `iterate_design` for the next round. A required Decision that is absent SHALL fail closed and MUST NOT silently materialize legacy `[8, 10, 12]` lengths.

#### Scenario: Required Decision is missing
- **WHEN** a formal Critic handoff contains a recommendation mapped to `iterate_design` and no matching formal Decision exists
- **THEN** Planner planning fails with an explicit missing-Decision contract error and no plan is persisted

#### Scenario: Bootstrap and legacy no-Decision paths
- **WHEN** initial bootstrap planning runs, or a direct compatibility caller invokes Planner without declaring a Decision requirement
- **THEN** the existing explicit no-Decision behavior remains available without ambient Decision discovery

### Requirement: Planner service forwards the explicit handoff to E3-A
The Planner service SHALL pass the explicitly supplied Decision to `build_plan` for the existing E3-A contract validation, local freezing, provenance binding, and identity binding. The service MUST NOT duplicate Decision validation or E3-B length materialization and MUST NOT directly read ambient experience or Evidence to obtain a Decision.

#### Scenario: Decision narrows all formal design jobs to length 12
- **WHEN** the formal service path receives a valid Decision with `preferred_lengths=[12]`
- **THEN** plan source contains the Decision ID, canonical Decision SHA-256, and Decision input digest, and every `iterate_design.design_jobs` entry has `lengths=[12]`

#### Scenario: No-adjustment Decision preserves a 10 and 12 approved envelope
- **WHEN** the formal service path receives a valid no-adjustment Decision whose approved effective envelope and proposed policy weights are `[10, 12]`
- **THEN** every `iterate_design.design_jobs` entry has `lengths=[10, 12]`

#### Scenario: Decision scope mismatch
- **WHEN** the supplied Decision mismatches the Planner project, workflow, source or applicable round, Prediction run, or target scope
- **THEN** the existing E3-A binding contract fails closed before plan persistence, including when Decision workflow identity differs from the formal Prediction workflow identity transported to Planner

### Requirement: Decision effects remain narrow and deterministic
For otherwise identical formal Planner inputs, a Decision SHALL change only E3-A plan identity/provenance and E3-B design-job lengths. Proposal count, route, target allocation, seeds, approvals, and other task policy SHALL remain unchanged, and repeated runs with the same inputs SHALL produce the same plan document.

#### Scenario: Compare Decision and no-Decision task policy
- **WHEN** equivalent Planner inputs are built with and without a valid narrowing Decision
- **THEN** the resulting design jobs differ only in their `lengths`, while proposal counts, routes, target allocation, seeds, and all unrelated task fields are equal

#### Scenario: Repeated formal handoff
- **WHEN** the same Store state, Critic artifact, Planner state snapshot, project configuration, and Decision are supplied repeatedly
- **THEN** Planner produces the same plan ID, source binding, tasks, allocations, and seeds
