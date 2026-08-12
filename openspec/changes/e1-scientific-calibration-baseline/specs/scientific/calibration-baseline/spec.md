## Purpose

Defines how a control-derived threshold snapshot becomes a project- and protocol-bound formal baseline, how simulation authority remains distinct from approved real scientific authority, and how Prediction proves which baseline it consumed.

## ADDED Requirements

### Requirement: Frozen calibration identity
A formally published E1 calibration snapshot SHALL carry one versioned identity envelope containing `calibration_authority`, project ID and approved project digest, the existing Prediction protocol name/version/integrity identity, scoring implementation name/version, threshold schema version, scored control dataset integrity identity, calibrated threshold snapshot integrity identity, deterministic publication ID, and calibration artifact ID/integrity identity. The vocabulary remains `simulation_only | approved_real`, but E1 SHALL publish and consume only `simulation_only`; `approved_real` SHALL fail closed at every formal boundary. Control dataset metadata, calibrator audit, publication, and later candidate evaluation SHALL carry the same Prediction-owned scoring implementation identity.

#### Scenario: Control and candidate use the same frozen identity
- **WHEN** scored controls are calibrated and a candidate is later evaluated against the approved result
- **THEN** the calibration snapshot and Prediction record identify the same calibration authority, project approval, Prediction protocol, scoring implementation, threshold schema, dataset, artifact, publication, and threshold snapshot

#### Scenario: Protocol identity differs
- **WHEN** a control dataset or calibration snapshot carries a protocol identity different from the active project-bound Prediction protocol
- **THEN** calibration publication or Prediction consumption fails closed before the result can be represented as approved calibration

### Requirement: Provenance-only controls do not imply calibration
An unscored control manifest SHALL remain an input provenance artifact and MUST NOT be treated as a scored control dataset or formal threshold authority. A schema-version-2 scored control dataset SHALL preserve each control's identity, positive or negative label, metric values, source/provenance, available assay or literature linkage, frozen protocol binding, Prediction scoring implementation identity, and `calibration_authority`. A metric SHALL be marked `calibrated` only when it satisfies the existing algorithm's sample-size, FPR, recall, separation, and metric-scope contract; otherwise it SHALL retain an existing non-calibrated status. Algorithmic `calibration_status=calibrated` and scientific authority remain independent fields.

#### Scenario: Sufficient bound controls calibrate a metric
- **WHEN** a project- and protocol-bound scored dataset contains sufficient positive and negative values for an eligible metric and satisfies the existing statistical contract
- **THEN** that metric is marked calibrated and its contributing control provenance is preserved in the calibration snapshot

#### Scenario: Controls are insufficient for a metric
- **WHEN** an eligible metric has fewer positive or negative scored controls than the existing contract requires
- **THEN** the metric is not marked calibrated and its prior threshold is not promoted as control-calibrated

#### Scenario: Provenance manifest has no scores
- **WHEN** a control manifest has labels and provenance but no frozen-protocol metric values
- **THEN** it remains an input artifact and produces no calibrated metric claim

### Requirement: Simulation calibration remains machine-distinguishable
A synthetic or scenario control dataset MAY exercise the existing calibrator and full formal publication/Prediction-consumption lifecycle only with `calibration_authority=simulation_only`. Synthetic provenance SHALL be explicit in dataset metadata and control records. The calibration artifact, Store binding, formal Evidence, Prediction cache identity, run manifest, candidate record, and Prediction Evidence SHALL preserve `simulation_only` without translating, defaulting, or omitting it. A dataset or control record marked synthetic MUST NOT be published or consumed as `approved_real`.

#### Scenario: Simulation controls complete the engineering lifecycle
- **WHEN** explicitly synthetic controls satisfy the unchanged calibration algorithm and are formally published
- **THEN** the artifact, Store state, Evidence, and Prediction outputs all retain `calibration_authority=simulation_only`

#### Scenario: Simulation attempts approved-real authority
- **WHEN** a synthetic dataset, synthetic control provenance, or simulation-only artifact is presented with `calibration_authority=approved_real`
- **THEN** publication or consumption fails closed before an approved-real claim becomes formal

#### Scenario: Simulation metric satisfies statistical calibration
- **WHEN** a simulation-only metric satisfies the existing sample, FPR, recall, and separation rules
- **THEN** its algorithmic calibration status MAY be `calibrated` while its scientific authority remains `simulation_only`

### Requirement: Dataset and project binding fail closed
The system SHALL validate scored control dataset integrity identity, declared calibration authority, project ID, approved project digest, and protocol binding before publication. The existing calibrator audit SHALL identify the exact input dataset, Prediction protocol identity/hash, and resolved calibration parameters including metric keys, target IDs, FPR, recall, and minimum positive/negative counts. Publication MUST reject an audit whose dataset, protocol, parameters, or calibrated threshold claims do not match, without rerunning the calibrator. A mismatch MUST leave the previously authoritative Store state unchanged and MUST NOT be recoverable through an unvalidated-threshold bypass.

#### Scenario: Project approval differs
- **WHEN** the scored control dataset project ID or approved project digest differs from the currently approved project
- **THEN** calibration publication fails closed without changing formal thresholds or their calibration binding

#### Scenario: Approved dataset content changes
- **WHEN** scored control dataset content no longer matches its approved integrity identity
- **THEN** calibration publication fails closed and records no approved calibration snapshot

#### Scenario: Dataset changes after calibration
- **WHEN** Dataset A produced Audit A and Threshold A but publication is attempted with changed Dataset B
- **THEN** publication fails closed because Audit A does not identify Dataset B

### Requirement: Prediction owns publication protocol and scoring identity
Calibration artifact creation and formal Store publication SHALL validate protocol and scoring implementation identity against the current values owned by `prediction_pipeline.protocol.protocol_binding()` and `prediction_pipeline.contracts.scoring_implementation_identity()`. Caller-supplied self-consistent alternate identities MUST fail closed before formal mutation.

#### Scenario: Caller supplies alternate protocol or scorer
- **WHEN** publication carries a fake protocol version or scoring implementation
- **THEN** creation or Store publication fails closed with no artifact authority, threshold mutation, or publication Evidence

### Requirement: Approved-real is unavailable in E1
E1 SHALL unconditionally reject `calibration_authority=approved_real` during artifact creation, Store publication, artifact validation, and Prediction consumption. A field injected under `project.review`, including `approved_scored_dataset_sha256`, SHALL NOT unlock real authority. E1 SHALL NOT create a real-control approval workflow.

#### Scenario: Structurally valid real dataset lacks external approval
- **WHEN** a non-synthetic dataset declares `approved_real` but the approved project authority has no matching approved scored-dataset digest
- **THEN** publication fails closed without changing formal Store state

#### Scenario: Mutable review data is injected after approval
- **WHEN** a matching scored-dataset digest is added under `project.review` without changing the approved content digest
- **THEN** approved-real publication still fails closed

### Requirement: Formal calibration requires canonical project approval and target scope
Builder, Store publication, and Prediction consumption SHALL require `review.status=approved` and a current matching approved content digest. Calibration parameter target IDs SHALL be a subset of the approved project's target IDs.

#### Scenario: Digest is valid but status is draft
- **WHEN** project review status is draft while the approved digest remains mathematically correct
- **THEN** builder, Store publication, and Prediction consumption fail closed

#### Scenario: Calibration target is outside the project
- **WHEN** calibration parameters name a target absent from the approved project
- **THEN** formal publication fails closed

### Requirement: Atomic formal calibration publication
Publishing a calibration SHALL atomically register the calibration artifact and its integrity identity, update the formal threshold snapshot and its calibration binding, and append formal calibration Evidence in SQLite. If any part fails, none of those formal records SHALL become visible. JSON or CSV files MAY serve as inputs or projections, but `state.json`, a threshold cache, or a calibration JSON file alone MUST NOT constitute formal application or runtime authority.

#### Scenario: Publication succeeds
- **WHEN** a valid approved calibration snapshot is published
- **THEN** the artifact, Evidence, thresholds, and calibration binding become visible together from the formal Store

#### Scenario: Publication fails partway
- **WHEN** artifact registration, state update, or Evidence append fails during publication
- **THEN** the Store exposes the previously committed calibration state with no partial new publication

#### Scenario: State projection is missing or changed
- **WHEN** `state.json` is absent or contains content different from the formal Store
- **THEN** calibration consumption derives authority from SQLite and does not accept the projection as an override

### Requirement: Calibration publication has deterministic idempotency
The publication ID SHALL be a deterministic natural identity derived only from canonical scientific/binding content, including calibration authority, project approval, protocol/scoring identity, dataset identity, calibration-parameters identity, threshold schema, and threshold snapshot identity. Publishing identical content again SHALL be an idempotent success only when the requested publication is already the current active authority and its publication Evidence, registered artifact, artifact file/integrity, thresholds, and binding are complete and identical. A superseded publication MUST NOT be reactivated by replay. Reusing the same publication ID for different binding, artifact, or threshold content MUST fail closed and preserve the prior publication.

#### Scenario: Identical publication is repeated
- **WHEN** the same canonical scientific/binding content is published more than once
- **THEN** publication returns the same identity and the Store retains one equivalent active calibration authority

#### Scenario: Publication identity collides with different content
- **WHEN** an existing publication ID is supplied with different binding, artifact, or threshold content
- **THEN** publication fails closed and the existing active calibration remains unchanged

#### Scenario: Superseded publication is replayed
- **WHEN** A is published, B supersedes A, and A is requested again
- **THEN** replay fails closed, B remains active, and publication Evidence chronology is unchanged

#### Scenario: Active authority is incomplete
- **WHEN** active binding and artifact metadata exist but publication Evidence or the registered artifact file/integrity is missing
- **THEN** replay does not return idempotent success and requires explicit recovery

#### Scenario: Publication Evidence payload is corrupt
- **WHEN** the expected publication Evidence exists but its calibration binding differs from the requested binding
- **THEN** replay fails closed without changing active State

### Requirement: Calibration artifact integrity is verified at consumption
The formal Store SHALL retain the calibration artifact integrity identity. Before a formally published snapshot is consumed, the system SHALL verify that the referenced artifact content and threshold snapshot still match their stored identities. A modified artifact or threshold snapshot MUST fail closed.

#### Scenario: Calibration artifact is modified
- **WHEN** the registered calibration artifact content differs from its approved integrity identity
- **THEN** Prediction rejects the approved-calibration claim before candidate evaluation

#### Scenario: Threshold snapshot differs
- **WHEN** runtime thresholds differ from the threshold snapshot bound to the approved calibration artifact
- **THEN** Prediction rejects the approved-calibration claim rather than silently evaluating under mixed thresholds

### Requirement: Prediction proves threshold consumption
Prediction SHALL read the formal calibration binding with the Store-owned thresholds, validate it against the current approved project and active Prediction protocol/scorer, and preserve the consumed identity in its cache identity, run manifest, candidate record, and formal Evidence. `PredictionPipeline` SHALL treat only the result of the formal validation seam as formal authority; a plain caller-provided dict SHALL NOT authorize calibrated thresholds. A provisional, unavailable, pending, insufficient, or not-separated metric MUST NOT be represented as algorithmically calibrated, and a `simulation_only` baseline MUST NOT be represented as `approved_real`.

#### Scenario: Direct Pipeline receives an unvalidated formal claim
- **WHEN** direct Pipeline construction receives calibrated thresholds with no validated binding or with a forged formal-looking dict
- **THEN** construction fails before candidate evaluation, Evidence, Candidate, or State mutation

#### Scenario: Prediction consumes a simulation snapshot
- **WHEN** Prediction evaluates a candidate using a valid simulation-only publication
- **THEN** its cache identity, run manifest, candidate record, and formal Evidence identify the exact consumed digests and retain `calibration_authority=simulation_only`

#### Scenario: Prediction uses no approved calibration snapshot
- **WHEN** Prediction evaluates with literature or provisional thresholds and no approved calibration publication
- **THEN** its record explicitly reports the absence of an approved calibration binding and does not claim those metrics were calibrated

#### Scenario: Store binding and active project differ
- **WHEN** the formal calibration binding project approval differs from the active approved project
- **THEN** Prediction fails closed before candidate evaluation

### Requirement: Frozen calibration is deterministic
Given the same scored control dataset content, frozen protocol identity, calibration parameters, target scope, and starting thresholds, repeated clean-environment calibration SHALL produce the same scientific threshold values, metric statuses, statistical results, and threshold snapshot integrity identity. Runtime timestamps, filesystem paths, Store-generated IDs, and other publication metadata SHALL NOT participate in the scientific snapshot identity.

#### Scenario: Clean-environment replay
- **WHEN** the same frozen inputs are calibrated in two clean environments
- **THEN** both runs produce identical scientific snapshot content and the same threshold snapshot integrity identity
