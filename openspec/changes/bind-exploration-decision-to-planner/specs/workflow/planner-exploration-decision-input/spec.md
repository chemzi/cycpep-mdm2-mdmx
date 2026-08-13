## Purpose

Define the explicit frozen handoff by which Planner binds one validated E2 ExplorationDecision into plan provenance and identity without applying the Decision to executable work.

## ADDED Requirements

### Requirement: Planner accepts only an explicit validated ExplorationDecision
Planner SHALL accept an optional explicit ExplorationDecision input. When supplied, Planner MUST restore it through the public ExplorationDecision contract and MUST fail closed if the contract or any Planner handoff binding is invalid. Planner MUST NOT reconstruct the Decision, duplicate its internal validator, or discover a Decision from Evidence, experience, State history, or another ambient source.

The validated Decision MUST match the plan's project ID, current approved project revision digest, workflow ID, source round, next applicable round, Prediction run ID, and Critic required-target scope. Target scope equality MUST be order-insensitive after the Critic scope is verified as a non-empty unique string sequence; this comparison MUST NOT reorder the Critic source or downstream task inputs.

#### Scenario: Valid explicit Decision binding
- **WHEN** a caller supplies a contract-valid Decision whose project, workflow, source round, applicable round, Prediction run, and target scope match the Critic/Planner inputs
- **THEN** Planner accepts that Decision as the frozen input for the plan

#### Scenario: Invalid Decision contract fails closed
- **WHEN** a caller supplies a Decision payload that the public ExplorationDecision contract rejects
- **THEN** Planner rejects plan construction without substituting another Decision

#### Scenario: Handoff identity mismatch fails closed
- **WHEN** the validated Decision mismatches the plan project ID, workflow ID, source round, applicable round, Prediction run ID, or required-target scope
- **THEN** Planner rejects plan construction before producing a plan

#### Scenario: Stale approved project revision fails closed
- **WHEN** a Decision was created under a different approved project revision digest than the current explicit project configuration
- **THEN** Planner rejects plan construction before freezing the Decision or producing a plan

#### Scenario: Equivalent reordered target scope is accepted without mutation
- **WHEN** the Decision target IDs and Critic required targets contain the same unique non-empty strings in different orders
- **THEN** Planner accepts the binding while preserving the Critic target order used by existing task construction

#### Scenario: Ambient Evidence cannot supply or replace the explicit Decision
- **WHEN** ambient Evidence or experience history contains a different or newer exploration Decision but the caller supplies one explicit Decision
- **THEN** Planner binds only the explicit Decision and performs no ambient Decision or experience lookup, even when approved target configuration omits explicit lengths

### Requirement: Planner canonically binds Decision identity to plan identity
For a supplied Decision, Planner MUST serialize the validated contract canonically, calculate the canonical Decision SHA-256 over that serialization, and bind both the Decision ID and canonical Decision SHA-256 into the Planner input digest. The same complete Planner inputs and same Decision MUST produce the same digest and plan ID; a different valid Decision ID or canonical payload MUST change the Planner input digest.

#### Scenario: Repeated input is deterministic
- **WHEN** Planner receives the same Critic report, State/configuration inputs, and valid Decision more than once
- **THEN** each plan has the same Decision provenance, input digest, and plan ID

#### Scenario: Different valid Decision changes plan identity
- **WHEN** all non-Decision Planner inputs are unchanged and a different valid bound Decision is supplied
- **THEN** the Planner input digest and plan ID differ

### Requirement: Planner records additive Decision provenance
For a plan built with a supplied Decision, Planner source MUST record the Decision ID, canonical Decision SHA-256, and the Decision's own input digest. These fields MUST describe the same validated Decision bound into the Planner input digest.

#### Scenario: Decision provenance is emitted
- **WHEN** Planner successfully builds a plan with an explicit Decision
- **THEN** the plan source includes `exploration_decision_id`, `exploration_decision_sha256`, and `exploration_decision_input_digest` matching that Decision

### Requirement: Decision absence preserves legacy plan identity and shape
When no Decision is supplied, Planner SHALL preserve the pre-change source object, input digest, and plan ID for identical legacy inputs. Planner MUST NOT emit Decision source properties with null values or inject a placeholder into the digest.

#### Scenario: Legacy call remains identical
- **WHEN** a caller invokes Planner without the optional Decision using inputs accepted by the frozen baseline
- **THEN** the produced source object, input digest, and plan ID equal the frozen-baseline result

### Requirement: Frozen Decision input is non-authoritative and non-operative in E3-A
Planner SHALL place the canonical Decision only in its private local State copy for the duration of plan construction. Planner MUST NOT persist it through State or Evidence and MUST NOT apply its adjustment to tasks, design jobs, proposal counts, lengths, seeds, approvals, orchestration, or execution.

Planner MUST treat `_frozen_exploration_decision` as an invocation-owned reserved key: any caller-supplied value under that key MUST be removed from the local copy before an explicitly supplied and validated Decision may be injected.

#### Scenario: Decision does not alter executable work
- **WHEN** two otherwise identical plans are built with different valid Decisions
- **THEN** their task lists, budget requests, approval requests, execution policy, proposal counts, lengths, and seeds remain identical while only Decision-bound identity/provenance differs

#### Scenario: Plan construction performs no formal persistence
- **WHEN** Planner builds a plan with a valid explicit Decision
- **THEN** no State update and no Evidence append occurs

#### Scenario: Missing configured lengths use a static non-Decision fallback
- **WHEN** Planner builds with an explicit Decision and a required target has no configured design lengths
- **THEN** task construction uses its existing static default lengths without consulting or recording ambient experience and without reading the Decision adjustment

#### Scenario: Legacy ambient fallback remains compatible
- **WHEN** Planner builds without an explicit Decision and a required target has no configured design lengths
- **THEN** the pre-change ambient-experience fallback remains available

#### Scenario: Ambient State cannot impersonate the explicit Decision path
- **WHEN** caller State contains a `_frozen_exploration_decision` value but no explicit Decision argument is supplied
- **THEN** Planner removes the ambient value from its local copy and preserves the legacy task-building path
