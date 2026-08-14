## Purpose

Provide operators with one project-scoped, read-only readiness command and an auditable documentation path from installation through formal Launcher startup.

## ADDED Requirements

### Requirement: Doctor evaluates the approved project without side effects
The system SHALL provide `python -m workflow doctor --project <path>` as a read-only command. It SHALL load the named project rather than an ambient default, require its approval digest to match its current content, and validate every required target's design-ready coordinate artifact and declared SHA-256 before declaring readiness, including approved legacy targets without a `structure_plan`. It MUST NOT create or modify Store rows, Evidence, artifacts, diagnostics, project files, configured runtime roots, approvals, or workflow runs.

#### Scenario: Approved project and coordinates are valid
- **WHEN** an operator runs doctor with an approved project whose required target coordinates exist and match their declared SHA-256
- **THEN** the project and coordinate checks pass without modifying the approved project or any formal runtime state

#### Scenario: Project authority is invalid
- **WHEN** the project is unapproved, has changed after approval, is malformed, or has a required target with missing or hash-mismatched coordinates
- **THEN** doctor reports a failed project-scoped check and does not continue as though the environment were ready

### Requirement: Doctor uses production runtime identities as its authority
Doctor SHALL resolve paths through the current production project and environment configuration seams and SHALL reuse the existing production scientific runtime validators where they define an expected version, commit, model, or checkpoint digest. It SHALL distinguish an identity that was exactly verified from a dependency that was only observed or checked for availability, and it MUST NOT invent an expected commit or checksum that production does not own.

#### Scenario: Complete scientific runtime is installed
- **WHEN** the configured RFdiffusion Python/repository, LigandMPNN repository and protocol-selected checkpoint, ColabDesign repository and AF parameters, Boltz executable/cache/checkpoint, PyRosetta Python, and PRODIGY executable satisfy their production requirements
- **THEN** doctor reports the resolved non-secret locations and the verified or observed identity of each dependency

#### Scenario: Scientific identity differs
- **WHEN** a production validator observes a wrong ColabDesign commit, Boltz version or checkpoint SHA-256, PyRosetta version, PRODIGY distribution version, or another formally enforced identity
- **THEN** doctor reports that exact check as failed and the overall result is not ready

#### Scenario: Runtime is present but has no enforced repository revision
- **WHEN** production requires a repository or model path but does not enforce a repository commit or byte digest
- **THEN** doctor reports availability and any safely observable revision without describing it as an exact production-verified identity

### Requirement: Doctor checks operational launch prerequisites
Doctor SHALL check the project-scoped SQLite/runtime locator inputs, configured execution/design/prediction/artifact roots, required Python entry points, CUDA installation, visible NVIDIA GPU, and write capability of roots that a new run must use. When the configured SQLite database exists, doctor SHALL use the supported read-only schema and project-binding preflight; when a fresh deployment has no database yet, doctor SHALL instead validate the explicit target location and usable existing parent without creating the database and SHALL report `store_will_initialize_on_launch`. The checks SHALL be bounded and SHALL NOT launch a scientific model or create a persistent probe file. This change defines one readiness profile, `fresh_full_launcher`, for which `OPENAI_API_KEY` is a required Research prerequisite; its value MUST never be emitted.

#### Scenario: Operational prerequisites are ready
- **WHEN** an existing Store passes its supported read-only preflight or a fresh Store target has a usable parent, all configured roots are usable, required entry points exist, CUDA and an NVIDIA GPU are visible, and the fresh full-Launcher Research credential is present
- **THEN** the operational checks pass without exposing credentials or starting scientific work

#### Scenario: Existing Store has invalid authority
- **WHEN** the configured SQLite file already exists but fails schema or project-binding validation
- **THEN** doctor reports the Store check as failed rather than treating it as a fresh database target

#### Scenario: GPU is not visible
- **WHEN** CUDA files exist but no NVIDIA GPU is visible to the process
- **THEN** doctor reports GPU visibility as failed and does not return ready

#### Scenario: Research credential is absent
- **WHEN** `OPENAI_API_KEY` is absent under the `fresh_full_launcher` profile
- **THEN** doctor reports only the credential name as missing, never its value, and does not return ready

### Requirement: Doctor produces deterministic actionable output
Doctor SHALL emit one bounded result per check with a stable check identifier, category, status, concise observation, and remediation owner or next action when not passing. Statuses SHALL distinguish `pass`, `fail`, `warning`, and `skipped`. The default operator view SHALL end in `READY` only when no required check failed, while `--json` SHALL expose the same result as one machine-readable document. The process SHALL exit zero only for `READY` and non-zero for invalid input or unmet required checks.

#### Scenario: All required checks pass
- **WHEN** every required check passes and conditional checks are either satisfied or legitimately skipped
- **THEN** the text output ends with `READY`, JSON reports `ready: true`, and the process exits zero

#### Scenario: A required check fails
- **WHEN** one or more required checks fail
- **THEN** all independent safe checks still run, the output identifies the first and remaining failures without a traceback, the final result is `NOT READY`, and the process exits non-zero

### Requirement: Deployment documentation separates user journeys
`README.md` SHALL provide a concise project orientation and the supported sequence for running an already provisioned environment: activate the environment, select the approved project and runtime configuration, run doctor, then invoke `python -m workflow launch --project ...`. It SHALL link to `docs/INSTALLATION.md` for new-machine provisioning and `THIRD_PARTY.md` for dependency audit details, and SHALL not label base `requirements.txt` installation as a complete scientific-runtime quick start.

#### Scenario: Operator has a provisioned machine
- **WHEN** an operator follows the README run instructions
- **THEN** they encounter the approval and doctor gates before Launcher and are not told that base Python installation alone is sufficient

### Requirement: Installation guide is reproducible and license-aware
`docs/INSTALLATION.md` SHALL document a new GPU-machine bootstrap in dependency order, including supported host/GPU assumptions, repository checkout, isolated Python environments, environment variables, repositories, model parameters and checkpoints, formal project coordinate materialization and approval, Store/runtime roots, doctor verification, launch, and troubleshooting ownership. It SHALL mark PyRosetta and any other restricted component as requiring a legitimate authorized source rather than offering a bypass, substitute, or redistributed package.

#### Scenario: New machine bootstrap
- **WHEN** an operator follows the installation guide using authorized dependency sources
- **THEN** every doctor requirement has a documented provisioning step or an explicit conditional/authorization boundary

### Requirement: Third-party inventory is auditable
`THIRD_PARTY.md` SHALL classify external components as required, conditional, or development-only and record, where applicable, the component/distribution name, production role, exact enforced or observed version/commit/checkpoint identity, runtime environment, configured selector, official upstream, installation source, citation, license or terms status, and whether the identity is machine-enforced. Unknown or team-specific license facts SHALL be labeled unresolved or locally authorized rather than guessed.

#### Scenario: Dependency identity is audited
- **WHEN** a reviewer inspects a scientific dependency in `THIRD_PARTY.md`
- **THEN** they can determine why it is present, how production selects it, what identity is expected or merely observed, and which upstream, citation, and license status apply

#### Scenario: Identity authority changes
- **WHEN** a production validator, protocol, or deployment pin changes
- **THEN** the corresponding documentation and doctor regression are required to change in the same review rather than leaving a stale standalone version table

### Requirement: Existing workflow behavior remains compatible
The change SHALL NOT alter the behavior, output, public handler construction, approvals, retries, recovery, scientific protocol, readiness contract, Store schema, or transaction semantics of `launch`, `status`, or `resume`. Running doctor is an explicit operator step and SHALL NOT automatically start Launcher or become an implicit mutation inside existing commands.

#### Scenario: Existing command is invoked
- **WHEN** an operator invokes `launch`, `status`, or `resume` without invoking doctor
- **THEN** its existing dispatch and JSON result contract remains unchanged
