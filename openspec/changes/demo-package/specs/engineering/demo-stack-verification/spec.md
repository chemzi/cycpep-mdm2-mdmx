## Purpose

Defines the demo package contract: a reproducible one-command stack verification and snapshot generation for the Frontend V2 read model.

## ADDED Requirements

### Requirement: One-command stack verification
The system SHALL provide a script that starts the local `web_api` adapter, fetches `/api/v2/workbench`, verifies the response schema version, prints a human-readable summary, and writes a snapshot JSON under `demo/snapshot/`.

#### Scenario: Read model is healthy
- **WHEN** the script runs against a store with current schema
- **THEN** it reports the workbench `schema_version`, project/targets, section counts, and blockers, and writes the snapshot

#### Scenario: Read model fails
- **WHEN** the endpoint returns an error
- **THEN** the script exits non-zero and leaves a readable error message; no partial snapshot is produced

### Requirement: Demo assets live under demo/
The README SHALL document startup, the pitch story, the honest data baseline, and the roadmap; the FAQ SHALL answer the expected defense questions without overclaiming learning or scientific pass.