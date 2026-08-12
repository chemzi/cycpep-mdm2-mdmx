## Purpose

Ensure production Prediction accepts the supported PRODIGY distribution layout and rejects unverifiable or version-incompatible runtimes before scientific execution begins.

## ADDED Requirements

### Requirement: Production validates the configured PRODIGY runtime
Before starting Prediction scientific subprocesses, the system SHALL validate PRODIGY with the Python interpreter located beside the configured PRODIGY executable. The probe SHALL import the supported `prodigy_prot` module, read the `prodigy-prot` distribution version through package metadata, and require an exact match with the version already bound by the active Prediction execution identity.

#### Scenario: Supported production package passes
- **WHEN** the configured executable has a sibling Python in which `prodigy_prot` imports successfully and `prodigy-prot` metadata reports `2.4.0`
- **THEN** runtime validation succeeds with observed version `2.4.0`
- **AND** validation does not require a module named `prodigy`

#### Scenario: Installed distribution version differs
- **WHEN** the configured runtime imports `prodigy_prot` but `prodigy-prot` metadata reports `2.3.x` while the bound version is `2.4.0`
- **THEN** validation fails closed with `prodigy_version_mismatch`
- **AND** Prediction scientific subprocesses do not start

### Requirement: PRODIGY probe failures are typed and diagnostic
The system SHALL fail closed when the configured sibling Python cannot import `prodigy_prot`, cannot read `prodigy-prot` distribution metadata, or otherwise exits unsuccessfully. The typed failure message SHALL include a bounded suffix of probe stderr sufficient to identify the runtime failure without exposing unbounded process output.

#### Scenario: Metadata matches but module import fails
- **WHEN** distribution metadata is present for version `2.4.0` but importing `prodigy_prot` fails
- **THEN** runtime validation reports a typed probe failure
- **AND** the bounded error includes the relevant import failure from stderr
- **AND** Prediction scientific subprocesses do not start

#### Scenario: Worker advances after the PRODIGY seam
- **WHEN** the Worker observes a valid runtime for the configured PRODIGY executable and all earlier Prediction runtime checks pass
- **THEN** the Worker records PRODIGY version `2.4.0` in its observed execution identity
- **AND** it proceeds to the next existing scientific execution seam without changing Prediction readiness or protocol semantics
