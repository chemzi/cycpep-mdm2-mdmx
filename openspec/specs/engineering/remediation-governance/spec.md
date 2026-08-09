# Remediation Governance Specification

## Purpose

Provide durable repository-level instructions and a long-term remediation decision framework without creating a second system for tracking individual engineering changes.

## Requirements

### Requirement: Repository remediation has a durable entrypoint
The repository SHALL track a correctly named root `AGENTS.md` that defines the development workflow and directs remediation work to the long-term remediation strategy.

#### Scenario: An agent starts repository remediation
- **WHEN** an agent reads the root repository instructions before remediation work
- **THEN** the instructions identify OpenSpec as the source of truth for individual changes and direct the agent to the remediation strategy for long-term direction

#### Scenario: Existing integrity contracts require hash verification
- **WHEN** repository guidance discourages unnecessary new hash checks as overdefense
- **THEN** hash and SHA256 behavior explicitly required by an existing protocol, artifact, or integrity contract remains mandatory and follows the existing design

### Requirement: Remediation strategy records durable decision context
The repository SHALL maintain `docs/engineering/remediation-strategy.md` with the audit-derived governance direction, prioritization principles, high-risk boundaries, and criteria for choosing the next remediation change.

#### Scenario: A completed change is reassessed
- **WHEN** a remediation change has been verified and archived
- **THEN** the strategy provides enough durable decision context to select the next smallest high-value problem without restarting a full-repository audit

#### Scenario: Evidence about the repository changes
- **WHEN** a future audit or verified change invalidates a durable architectural assumption in the strategy
- **THEN** the strategy is updated through a separately scoped documentation or governance change rather than silently preserving the stale assumption

### Requirement: OpenSpec remains the only per-change tracker
The remediation strategy SHALL describe direction and selection rules but SHALL NOT contain a task list, implementation checklist, progress table, or authoritative status ledger for individual remediation changes.

#### Scenario: Concrete remediation work is selected
- **WHEN** maintainers choose a specific non-trivial remediation problem
- **THEN** its scope, requirements, design, tasks, approvals, and progress are recorded only in an independent OpenSpec change

#### Scenario: Strategy lists known problem areas
- **WHEN** the strategy identifies a known debt family or high-risk boundary
- **THEN** it describes the concern and selection constraints without assigning per-change completion state

### Requirement: Governance persistence does not change runtime behavior
Persisting the governance documents SHALL NOT modify production code, public interfaces, CLI behavior, business logic, scientific behavior, persistence behavior, transaction behavior, or data formats.

#### Scenario: Governance change is reviewed
- **WHEN** the implementation diff is compared with its approved OpenSpec artifacts
- **THEN** only `AGENTS.md`, `docs/engineering/remediation-strategy.md`, and the change's OpenSpec artifacts are changed

#### Scenario: Unrelated documentation drift is encountered
- **WHEN** README status wording, PR3/PR4 status, Web GUI documentation, or other documentation drift is observed during implementation
- **THEN** it remains unchanged and is deferred to a separate `documentation-reality-alignment` change
