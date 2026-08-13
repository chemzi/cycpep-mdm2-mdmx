## Purpose

Preserves the approved project and coordinate authority across the executable `iterate_design` subprocess boundary without creating a second project authority.

## ADDED Requirements

### Requirement: Iterate Design SHALL consume the Planner-bound approved project
Before starting any `iterate_design` scientific job, Execution SHALL resolve the approved project from the formal Store-backed state used by the production handler and validate it against the task's `project_config_digest`. Every Design subprocess in that invocation SHALL consume that exact validated project config, including each required target's coordinate path, coordinate binding, PDB identity, and chain. Its explicit Design context and its legacy process-scoped project lookup SHALL be projections of the same invocation snapshot.

#### Scenario: Coordinate-bound project reaches every Design job
- **WHEN** an approved `iterate_design` task contains multiple Design jobs and its project digest matches the current approved project
- **THEN** every job receives the same snapshot through both the explicit command input and process-scoped project selector, and resolves its target, Candidate ID, Store, and Evidence behavior from that config rather than an ambient or bundled default

#### Scenario: Project drift is rejected before subprocess launch
- **WHEN** the current approved project does not match the task's `project_config_digest`
- **THEN** Execution fails with the existing project-drift contract before creating the project handoff or launching any Design subprocess

#### Scenario: Ambient default differs from Store-backed project
- **WHEN** the formal Store-backed approved project is coordinate-bound but the ambient or bundled default names a different project without those bindings
- **THEN** every Design child still uses the Store-backed snapshot for its explicit context and legacy process-scoped data paths

### Requirement: Explicit Design project input SHALL fail closed
When the Design command is given an explicit project-config input, it SHALL construct its Design context from that input. A missing, unreadable, malformed, or invalid explicit project config MUST fail before any scientific route executes and MUST NOT fall back to an environment-selected or bundled project.

#### Scenario: Explicit project config is valid
- **WHEN** the Design command receives a valid approved project config with formal coordinate bindings
- **THEN** its selected route observes that project's targets and coordinate bindings

#### Scenario: Explicit project config is invalid
- **WHEN** the Design command receives an invalid explicit project-config input
- **THEN** the command fails before route execution and does not use a default project

#### Scenario: Legacy command omits explicit project config
- **WHEN** a legacy Design command does not provide an explicit project-config input
- **THEN** its existing environment/default project resolution behavior remains unchanged

### Requirement: Project handoff SHALL preserve invocation atomicity
The project handoff SHALL be invocation-local and non-authoritative. Candidate updates from all Design jobs SHALL remain staged under the existing transaction boundary, and a failure in any job SHALL leave the formal Candidate Store and authoritative candidate-registration Evidence unchanged.

#### Scenario: Later job fails after an earlier staged update
- **WHEN** one Design job emits staged candidate updates and a later job fails
- **THEN** the invocation fails without publishing any staged candidate, candidate-registration Evidence, or successful Design result

### Requirement: Project handoff SHALL NOT change scientific or governance policy
The handoff SHALL NOT alter Design scientific parameters, Planner task scope, approval budgets, retry behavior, Candidate/Store schemas, or workflow sequencing.

#### Scenario: Existing task controls remain authoritative
- **WHEN** an `iterate_design` task is executed through the corrected handoff
- **THEN** route, target, proposal count, lengths, seed, candidate limit, approval, and transaction controls remain those already bound by the task and approval
