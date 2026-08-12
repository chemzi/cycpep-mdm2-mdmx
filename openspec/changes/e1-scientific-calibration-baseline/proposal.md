## Why

The repository can calculate control-derived thresholds, but it cannot yet prove which formally published calibration snapshot a Prediction verdict consumed or distinguish an engineering simulation from approved real scientific calibration. E1 freezes that provenance and authority chain without changing the scientific calibration algorithm or claiming that unscored MDM2/MDMX controls are calibrated.

## What Changes

- Freeze one calibration identity contract around the existing Prediction protocol binding, threshold schema version, scoring implementation identity, approved project digest, control dataset digest, calibration artifact digest, calibrated threshold snapshot digest, and an explicit `calibration_authority` of `simulation_only` or `approved_real`.
- Allow synthetic/scenario controls to exercise the existing calibrator and full publication/consumption lifecycle only as `simulation_only`; preserve that authority in the dataset, artifact, Store binding, Evidence, cache identity, and Prediction record so it cannot be represented as experimentally or scientifically approved calibration.
- Treat the existing MDM2/MDMX control manifest as provenance-only input until the controls are scored by the frozen Prediction protocol and satisfy the existing sample, FPR, recall, and separation requirements; metrics that do not satisfy the contract remain explicitly provisional or unavailable.
- Validate the project, protocol, control authority, and content bindings before publication and fail closed when the dataset, project approval, protocol, authority, threshold snapshot, or calibration artifact does not match.
- Add a narrow SQLite Store operation that atomically publishes the calibration artifact registration, formal calibration Evidence, calibrated thresholds, and their authoritative calibration binding. JSON files remain input artifacts or projections, never runtime authority.
- Require Prediction to validate the Store-owned calibration binding against the active project, protocol, threshold snapshot, and artifact content, then preserve that binding in run manifests, candidate records, and formal Evidence.
- Define a deterministic publication natural identity: replaying identical scientific/binding content is idempotent, while reusing that identity for different content fails closed.
- Bind the existing calibrator audit to the exact scored dataset, active Prediction protocol, and resolved calibration parameters so publication cannot combine Dataset B with Audit/Threshold A.
- Limit idempotency to an exact replay of the currently active and complete formal authority; replay of a superseded publication fails closed and does not reactivate it.
- Fail closed for `approved_real` until the approved project authority freezes an `approved_scored_dataset_sha256` matching the exact scored dataset.
- Add one end-to-end deterministic simulation test from controls through the existing calibrator, simulation-only artifact, atomic Store publication, and exact Prediction consumption, including mismatch/tamper regressions.
- Limit the E1 MVP to the versioned binding contract, atomic SQLite publication, and Prediction validation/recording. Preserve the existing calibration algorithm, real-control research, project configuration, UI, Launcher, Planner, Execution, Store schema, Research stages, Frontend, Exploration, and all E2+ work unchanged.

## Capabilities

### New Capabilities

- `scientific/calibration-baseline`: Defines the frozen approved calibration snapshot, formal publication, and verifiable Prediction consumption contract layered on the existing control-threshold algorithm.

### Modified Capabilities

None. The completed but unarchived `p0c-threshold-calibration` change remains the source for the control-calibration algorithm contract; E1 does not duplicate or modify those requirements.

## Impact

- Expected implementation surface: a small calibration-baseline contract, the existing SQLite Store/data-layer boundary, Prediction pipeline records/effects, calibration documentation, and focused tests. No project config, scoring script, control manifest, UI, Launcher, Planner, Execution, or Store schema change is planned.
- Public behavior changes: Prediction refuses a threshold snapshot that claims formal calibration but lacks or mismatches its Store binding; successful Prediction records and Evidence expose the consumed authority/calibration/threshold identity.
- Data format changes: calibration artifacts and Store state gain a versioned binding envelope with `calibration_authority` and deterministic `publication_id`; Prediction run/record/Evidence payloads gain the consumed binding. Existing uncalibrated thresholds remain readable under the existing hard-clearance rules.
- Migration: no historical record is relabelled. Existing projections and unbound calibration JSON remain non-authoritative; a calibration must be republished through the new Store boundary before Prediction may claim it as approved calibration.
- No dependency, scientific algorithm, sample-size, FPR, recall, or metric-direction change is introduced.
