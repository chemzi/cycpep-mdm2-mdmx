## Why

The repository can calculate control-derived thresholds, but it cannot yet prove which formally published calibration snapshot a Prediction verdict consumed or distinguish an engineering simulation from approved real scientific calibration. E1 freezes that provenance and authority chain without changing the scientific calibration algorithm or claiming that unscored MDM2/MDMX controls are calibrated.

## What Changes

- Freeze one simulation-only calibration identity contract around the existing Prediction protocol binding, threshold schema version, scoring implementation identity, approved project digest, control dataset digest, calibration artifact digest, and calibrated threshold snapshot digest. The schema retains `simulation_only | approved_real`, but E1 unconditionally rejects `approved_real`; real authority is a future formal approval capability.
- Allow synthetic/scenario controls to exercise the existing calibrator and full publication/consumption lifecycle only as `simulation_only`; preserve that authority in the dataset, artifact, Store binding, Evidence, cache identity, and Prediction record so it cannot be represented as experimentally or scientifically approved calibration.
- Treat the existing MDM2/MDMX control manifest as provenance-only input until the controls are scored by the frozen Prediction protocol and satisfy the existing sample, FPR, recall, and separation requirements; metrics that do not satisfy the contract remain explicitly provisional or unavailable.
- Validate the project, protocol, control authority, and content bindings before publication and fail closed when the dataset, project approval, protocol, authority, threshold snapshot, or calibration artifact does not match.
- Add a narrow SQLite Store operation that atomically publishes the calibration artifact registration, formal calibration Evidence, calibrated thresholds, and their authoritative calibration binding. JSON files remain input artifacts or projections, never runtime authority.
- Require Prediction to validate the Store-owned calibration binding against the active project, protocol, threshold snapshot, and artifact content, then preserve that binding in run manifests, candidate records, and formal Evidence.
- Define a deterministic publication natural identity: replaying identical scientific/binding content is idempotent, while reusing that identity for different content fails closed.
- Bind the existing calibrator audit to the exact scored dataset, active Prediction protocol, and resolved calibration parameters so publication cannot combine Dataset B with Audit/Threshold A.
- Limit idempotency to an exact replay of the currently active and complete formal authority; replay of a superseded publication fails closed and does not reactivate it.
- Fail closed unconditionally for `approved_real`; adding mutable data under `project.review` cannot unlock it, and E1 does not implement a real-control approval authority.
- Bind scored-dataset metadata and calibrator audit to the current Prediction scoring implementation, require canonical approved project status, constrain calibration targets to approved project targets, and make direct Pipeline callers prove formal binding validation rather than trusting a dict.
- Add one end-to-end deterministic simulation test from controls through the existing calibrator, simulation-only artifact, atomic Store publication, and exact Prediction consumption, including mismatch/tamper regressions.
- Rebase onto the PR71 Prediction transaction baseline while preserving its raw-threshold semantics, `prediction_run_id`, candidate coverage, and cache/resume battery; layer validated calibration binding onto those contracts without reverting them.
- Add one Calibration → Prediction → ExplorationDecision integration regression proving that the consumed validated binding survives the PR71 Prediction transaction/effect path into the existing exploration decision input.
- Limit the E1 MVP to the versioned binding contract, atomic SQLite publication, Prediction validation/recording, and this boundary integration proof. Preserve the existing calibration algorithm, real-control research, project configuration, UI, Launcher, Planner, Execution, Store schema, Research stages, Frontend, Exploration behavior, and all E2+ work unchanged.

## Capabilities

### New Capabilities

- `scientific/calibration-baseline`: Defines the frozen approved calibration snapshot, formal publication, and verifiable Prediction consumption contract layered on the existing control-threshold algorithm.

### Modified Capabilities

None. The completed but unarchived `p0c-threshold-calibration` change remains the source for the control-calibration algorithm contract; E1 does not duplicate or modify those requirements.

## Impact

- Expected implementation surface: a small calibration-baseline contract, the existing SQLite Store/data-layer boundary, Prediction pipeline records/effects, calibration documentation, and focused tests. No project config, scoring script, control manifest, UI, Launcher, Planner, Execution, or Store schema change is planned.
- Public behavior changes: Prediction refuses a threshold snapshot that claims formal calibration but lacks a validated Store binding; ordinary dicts are not formal authority. Successful simulation Prediction records and Evidence expose the consumed authority/calibration/threshold identity.
- Data format changes: schema-version-2 scored dataset metadata requires `scoring_implementation`, which the calibrator audit preserves unchanged. Calibration artifacts and Store state retain the versioned binding envelope; existing uncalibrated thresholds remain readable under the existing hard-clearance rules.
- Migration: no historical record is relabelled. Existing projections and unbound calibration JSON remain non-authoritative; a calibration must be republished through the new Store boundary before Prediction may claim it as approved calibration.
- No dependency, scientific algorithm, sample-size, FPR, recall, or metric-direction change is introduced.
- Integration migration: the implementation is rebased onto `integration/data-integrity-transaction@b910811`; PR71's Prediction transaction/event semantics remain authoritative and the calibration binding is additive.
