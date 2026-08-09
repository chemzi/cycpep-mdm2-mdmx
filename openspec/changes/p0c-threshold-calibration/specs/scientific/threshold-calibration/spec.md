## Purpose

Defines the behavior contract for scientific threshold calibration: how positive/negative control datasets are validated and used, which metrics are calibration-eligible, how calibration results persist through the formal store, and how uncalibrated metrics stay distinct from hard scientific clearance.

## ADDED Requirements

### Requirement: Control dataset contract and provenance
The system SHALL accept a positive/negative control dataset only when it declares a schema version, a project binding, a scoring-protocol binding, and per-record provenance (role and reference identifier such as PDB ID or DOI). Control records SHALL be labelled `positive` or `negative`. A dataset missing required binding or provenance fields SHALL be rejected and MUST NOT replace existing thresholds.

#### Scenario: Dataset with complete provenance is accepted
- **WHEN** a control dataset declares schema version, project binding, protocol binding, and every record has a role and a reference identifier
- **THEN** the dataset is accepted for calibration

#### Scenario: Record missing provenance is rejected
- **WHEN** a control record lacks a role or a reference identifier
- **THEN** the dataset is rejected and existing thresholds are retained

### Requirement: KEAP1 experimental positives as canonical controls
The system SHALL provide the KEAP1 canonical cyclic-peptide series (PDB 7K2E, 7K2F, 7K2G, 7K2H, 7K2I, 7K2M; DOI 10.1021/jacs.0c09799) as labelled positive controls with their structural references and provenance.

#### Scenario: KEAP1 positives are available to calibration
- **WHEN** a user runs threshold calibration against the KEAP1 benchmark
- **THEN** the six experimental binders are available as positive controls with PDB and DOI provenance

### Requirement: Core metric calibration scope
The system SHALL restrict positive/negative replacement to the declared core calibration metrics: an explicit fixed subset of the battery that decides hard scientific clearance. The current core set SHALL be `L2_ipsae`, `L4_nc_term_dist`, `L5_hotspot_coverage`, `L6_pose_rmsd`, and `L7_scrmsd`. All other metrics SHALL retain their literature/team values and MUST NOT be replaced by control calibration, regardless of control separation.

#### Scenario: Core metric is calibrated
- **WHEN** a control dataset provides sufficient positive and negative controls for a core metric
- **THEN** the core metric threshold is replaced with the calibrated cutoff, marked with `calibration_status=calibrated`, and its control provenance is recorded

#### Scenario: Non-core metric is never replaced
- **WHEN** a control dataset separates well on a non-core metric
- **THEN** the non-core threshold keeps its existing literature/team value and its calibration status remains unchanged

### Requirement: Calibration persistence through the formal store
The system SHALL persist calibration results through formal records: the calibration audit record (`_threshold_calibration.json`) SHALL be registered in the artifact registry, calibration evidence SHALL be recorded as formal evidence events, and threshold state SHALL be updated through the store transaction-compatible state path (`sync_thresholds_from_cache` → SQLite `replace_state`). The Research threshold cache (`_thresholds_cache.json`) SHALL be the durable recovery source for threshold state, with `state.json` as its persisted SQLite projection; a JSON cache file alone SHALL NOT constitute a formal calibration record.

#### Scenario: Calibration output is registered as an artifact
- **WHEN** a calibration run completes successfully
- **THEN** the calibration audit record is registered in the artifact registry and a formal evidence event records the calibration summary

#### Scenario: Threshold state update goes through the store
- **WHEN** calibrated thresholds are applied to project state
- **THEN** the update is written through the store state path (`sync_thresholds_from_cache` → SQLite `replace_state`), and `state.json` is the persisted projection of the durable threshold cache

#### Scenario: JSON cache alone is not a formal calibration record
- **WHEN** a JSON calibration cache file is present without a corresponding artifact and evidence record
- **THEN** the file alone is not accepted as a formal calibration record; the formal record requires the artifact row and the evidence event alongside the store state update

### Requirement: Uncalibrated metrics distinguished from hard clearance
The system SHALL assign every threshold an explicit calibration status (`calibrated`, `pending`, `unavailable`, or `not_separated`). A metric that is not calibrated SHALL NOT contribute to hard scientific clearance. The system SHALL expose uncalibrated metrics through a read-only soft desirability / relative-ranking view that is clearly separate from hard clearance results.

#### Scenario: Uncalibrated threshold blocks hard clearance
- **WHEN** a candidate is evaluated against a clearance metric that is not calibrated
- **THEN** hard scientific clearance is denied for that metric and the evaluation reports it as not cleared

#### Scenario: Soft view is separate from hard clearance
- **WHEN** a metric is uncalibrated
- **THEN** it is available only through the soft desirability / relative-ranking view and is not reported as a hard pass
