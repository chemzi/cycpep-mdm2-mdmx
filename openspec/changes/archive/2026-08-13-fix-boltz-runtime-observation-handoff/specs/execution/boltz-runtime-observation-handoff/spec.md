## Purpose

Ensure that a successfully validated Boltz runtime identity is carried through one scientific execution into its authoritative artifact metadata without changing the approved scientific protocol.

## ADDED Requirements

### Requirement: Validated runtime observations are handed off intact

The Boltz scientific executor SHALL use the version and checkpoint observation returned by its successful runtime validator as the runtime identity for the prepared execution and final result metadata.

#### Scenario: Successful preparation retains runtime observations

- **WHEN** the configured Boltz runtime validator succeeds with an exact version and checkpoint observation
- **THEN** preparation completes without an undefined runtime-identity error
- **AND** the prepared execution exposes those exact validated observations

#### Scenario: Public scientific execution publishes validated identity

- **WHEN** one Boltz prediction completes with valid scientific outputs after successful runtime validation
- **THEN** its result metadata reports the exact validated tool version and checkpoint observation
- **AND** the approved checkpoint path and versioned protocol parameters used by the scientific command remain unchanged

#### Scenario: Enrichment can continue beyond Boltz

- **WHEN** Prediction enrichment receives a successful Boltz result carrying the validated runtime identity
- **THEN** it can hand the generated complex artifact to the next existing enrichment seam without reconstructing runtime identity

### Requirement: Runtime handoff repair preserves surrounding contracts

The repair SHALL NOT change Prediction retry semantics, base-artifact promotion, readiness, budget accounting, execution identity, Store schema, or scientific protocol.

#### Scenario: Existing orchestration and scientific policy remain authoritative

- **WHEN** the runtime observation handoff is repaired
- **THEN** all surrounding orchestration, transaction, readiness, budget, and scientific-policy decisions continue to use their existing owners and contracts
