## Purpose

Ensure committed bootstrap Prediction exposes the exact immutable threshold snapshot needed by downstream E3 publication without consulting staging or mutable State.

## ADDED Requirements

### Requirement: Bootstrap Prediction commits its threshold snapshot
The system SHALL commit the Prediction-time threshold snapshot as an additional Artifact in the same transaction as the Prediction handoff. Formal Prediction completion Evidence SHALL bind its artifact identity using `thresholds_artifact_id` and SHALL retain the existing canonical `thresholds_digest`; the Store Artifact row SHALL be the sole authority for committed path and byte SHA-256. The action SHALL retain `prediction_handoff` as its sole semantic output role, and the canonical `thresholds_digest` SHALL remain distinct from the Artifact byte SHA-256.

#### Scenario: Successful Prediction transaction
- **WHEN** an approved bootstrap Prediction task commits a handoff
- **THEN** exactly one threshold snapshot Artifact is committed with the handoff
- **AND** the formal handoff Evidence identifies that Artifact and the separate canonical threshold digest
- **AND** the Store Artifact owns its committed path and byte SHA-256

#### Scenario: Threshold snapshot cannot be committed
- **WHEN** the threshold snapshot is missing, malformed, or differs from the handoff threshold digest
- **THEN** the Prediction transaction fails before authoritative completion publication

### Requirement: Bootstrap readiness validates the committed threshold locator
The bootstrap Prediction owner SHALL return a completed boundary only when the handoff Evidence is selected by the boundary's named `handoff_artifact_id`, its `thresholds_artifact_id` resolves through the Store to an Artifact in the same committed transaction and producer task, the committed file matches the Store Artifact byte SHA-256, and its parsed canonical threshold digest matches both the Prediction handoff and formal Evidence authority.

#### Scenario: Exact committed threshold locator
- **WHEN** the committed handoff and threshold Artifact have matching task, transaction, artifact identity, file SHA-256, and canonical digest bindings
- **THEN** the completed Prediction boundary exposes the threshold snapshot locator

#### Scenario: Missing or conflicting locator
- **WHEN** the threshold Artifact or any binding is missing or conflicting
- **THEN** bootstrap Prediction readiness fails closed before Critic or Planner proceeds

### Requirement: E3 publication consumes only formal thresholds
E3 publication SHALL resolve the formal handoff Evidence by the handoff Artifact identity carried by the owner-validated completed Prediction boundary, SHALL read the threshold snapshot from that same boundary, and SHALL NOT reconstruct it from current State or read it from attempt staging.

#### Scenario: Formal locator is available
- **WHEN** current Prediction completion provides a validated threshold snapshot locator
- **THEN** E3 publication uses that snapshot and rechecks its existing authoritative digest

#### Scenario: Formal locator is unavailable
- **WHEN** current Prediction completion lacks the validated threshold snapshot locator
- **THEN** E3 publication fails closed without publishing a shortlist or ExplorationDecision

#### Scenario: Transactional handoff Evidence has no path
- **WHEN** bootstrap completion Evidence identifies the handoff and threshold snapshot only by Artifact IDs
- **THEN** E3 publication associates the handoff by `handoff_artifact_id` and does not require an Evidence `handoff_path`

### Requirement: Legacy direct Prediction remains compatible
The system SHALL preserve the existing direct Prediction owner path, whose completed run directory already contains the threshold snapshot adjacent to its handoff.

#### Scenario: Direct Prediction completion
- **WHEN** a non-bootstrap direct Prediction completes under the legacy run contract
- **THEN** its existing handoff-adjacent threshold resolution remains valid without a Store migration
