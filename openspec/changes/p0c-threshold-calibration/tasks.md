## 1. Control dataset contract v2 and KEAP1 controls (D1)

- [x] 1.1 Add `CONTROL_DATASET_VERSION = 2` in `threshold_calibration.py`; make `validate_control_metadata` accept schema versions {1, 2} while new calibration writes emit v2
- [x] 1.2 Enforce per-record provenance for v2 datasets (each record has `role` positive/negative and a `source` reference: `pdb_id` and/or `doi` and method); reject without `ControlDataError`
- [x] 1.3 Add `benchmarks/keap1/calibration/positive_controls.json` derived from `runtime_manifest.json` (6 PDB positives, provenance PDB + DOI + role)
- [x] 1.4 Add `scripts/prepare_keap1_controls.py` generating the full control manifest with deterministic sequence-permutation negatives labelled `in_silico_sequence_negative_control`
- [x] 1.5 Add `scripts/score_control_dataset.py` converting a control manifest into a bound v2 scored control dataset via the prediction pipeline (server-side run; unit-tested conversion)
- [x] 1.6 Add tests: v1 datasets still load, v2 provenance missing is rejected, manifest generator output is deterministic and well-formed

## 2. Core metric calibration scope (D2)

- [x] 2.1 Add `CALIBRATION_METRIC_KEYS = {"L2_ipsae", "L4_nc_term_dist", "L5_hotspot_coverage", "L6_pose_rmsd", "L7_scrmsd"}` in `threshold_calibration.py`
- [x] 2.2 Add optional `metric_keys` parameter to `calibrate_thresholds` (defaults to the core set); non-eligible metrics are skipped with audit reason `not_calibration_eligible` and thresholds untouched
- [x] 2.3 Add tests: core metric calibrated when controls separate, non-core metric never replaced regardless of separation

## 3. Formal-store persistence (D3)

- [x] 3.1 Add `State.register_artifact(...)` in `data_layer.py` delegating to `SQLiteStore.register_artifact`
- [x] 3.2 Register `_threshold_calibration.json` as artifact type `threshold_calibration` in `agents/research.py::_apply_control_calibration` after successful calibration, alongside the existing evidence event
- [x] 3.3 Document that JSON caches (`_threshold_calibration.json`, `_threshold_cache.json`) are projections/compatibility surfaces, not the formal write entry
- [x] 3.4 Add tests: successful calibration registers an artifact, records formal evidence, and state thresholds update through the store

## 4. Status semantics and soft desirability (D4)

- [x] 4.1 Document canonical `calibration_status` values (`calibrated`, `pending`, `unavailable`, `not_separated`) in `threshold_contract.py`
- [x] 4.2 Add read-only `soft_desirability(candidate, thresholds, target_ids)` helper returning per-metric `{value, desirability, calibration_status, hard_eligible, reason}` with `hard_eligible` mirroring `_threshold_is_justified`
- [x] 4.3 Add tests: uncalibrated metrics are soft-only and never affect `competition_clearance`; soft view is clearly separate from hard clearance

## 5. Documentation and validation

- [x] 5.1 Add threshold-calibration doc note (v2 contract, core scope, soft/hard distinction, negative-control limitation)
- [x] 5.2 Run focused tests (`test_threshold_calibration.py`, `test_threshold_research.py`, new tests), full applicable suite, and the architecture gate
