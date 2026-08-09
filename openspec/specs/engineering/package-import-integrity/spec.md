# Package Import Integrity Specification

## Purpose

Ensure importable Agent packages behave as stable modules rather than process-wide environment bootstraps, while preserving supported imports and enforcing the rule in CI.

## Requirements

### Requirement: Agent package imports preserve the import search path
Importing the public Critic, Planner, or Orchestrator Agent package SHALL NOT add, remove, or reorder entries in the caller's Python import search path.

#### Scenario: Importing supported Agent packages
- **WHEN** a caller imports the public Critic, Planner, and Orchestrator packages from a supported repository-root or module invocation
- **THEN** the import search path after each import is identical to the path immediately before that import

#### Scenario: Importing more than one Agent package
- **WHEN** a process imports the three Agent packages in any order
- **THEN** each package exposes its documented public names without relying on a path mutation performed by a previously imported package

### Requirement: Existing Agent interfaces remain compatible
The change SHALL preserve the documented public Python imports and legacy command-line entrypoints for Critic, Planner, and Orchestrator.

#### Scenario: Public names remain importable
- **WHEN** a caller imports the existing public classes, constants, and workflow functions from any affected Agent package
- **THEN** the same names remain available with unchanged call contracts

#### Scenario: Legacy CLI shim remains available
- **WHEN** a caller invokes an existing root-level Agent CLI shim using its documented command
- **THEN** the shim continues to delegate to the corresponding package CLI

### Requirement: Architecture Gate rejects package initializer path bypasses
The Architecture Gate SHALL report repository package initializer files that directly mutate Python's import search path, and a newly introduced violation SHALL fail the gate.

#### Scenario: Package initializer inserts a path
- **WHEN** a scanned package initializer directly inserts or appends an entry to the import search path
- **THEN** the Architecture Gate reports the file as an import-path mutation violation

#### Scenario: New violation is not baselined
- **WHEN** an import-path mutation violation is absent from the accepted architecture baseline
- **THEN** the Architecture Gate exits unsuccessfully and identifies the violation as new

#### Scenario: Standalone entrypoint is scanned
- **WHEN** a standalone CLI, worker startup module, script, or external-tool adapter configures an import path outside a package initializer
- **THEN** this focused rule does not classify that file as a package-initializer violation

### Requirement: Governance change preserves business state
Removing the package-level import bypass and adding its gate SHALL NOT change workflow decisions, scientific results, persisted records, transaction behavior, or formal data formats.

#### Scenario: Existing regression suite runs after the governance change
- **WHEN** the repository's CPU test suite and Architecture Gate run after implementation
- **THEN** existing business-behavior tests pass and the gate reports no new architecture violations
