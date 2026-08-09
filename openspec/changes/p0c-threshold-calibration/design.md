## Context

Current implementation baseline (see proposal.md for motivation and specs for requirements):

- `threshold_calibration.py` provides `calibrate_threshold(s)`, `load_control_dataset`, `coerce_dataset`, `validate_control_metadata`, `ControlDataError`, and `METRIC_SPECS` covering all 9 battery metrics. Control datasets are bound to the approved project (`project_id`, `approved_digest`) and scoring protocol (`protocol`/`protocol_hash`, schema `CALIBRATION_SCHEMA_VERSION=1`).
- `agents/research.py::_apply_control_calibration` optionally reads a control dataset (`_calibration_controls.json` or config/env path), calibrates, writes `_threshold_calibration.json` and the threshold cache, and logs a `threshold_calibration` evidence event.
- `data_layer.py::State.sync_thresholds_from_cache` reads the JSON threshold cache, merges it via `threshold_contract`, and writes state through the SQLite store (`replace_state`); `state.json` is already a projection.
- `storage/sqlite_store.py` exposes `register_artifact` (artifacts table) and formal evidence append; `register_artifact` currently has no production callers.
- `battery_evaluation.py::_threshold_is_justified` already blocks hard clearance for thresholds without a value, source, calibration, or credible evidence grade; `competition_clearance` requires every metric justified.
- `benchmarks/keap1` ships 6 experimental positive controls (PDB 7K2E/7K2F/7K2G/7K2H/7K2I/7K2M, DOI 10.1021/jacs.0c09799) with structures and role `experimental_positive_control`, labels otherwise withheld. `docs/keap1_backend_validation_20260803.md` §6 plans a KEAP1 calibration set of 6 structural positives plus conformational/sequence negatives, prioritizing L2/L5/L6.
- Baseline tests: `test_threshold_calibration.py`, `test_threshold_research.py` (cache merge/status), architecture gate with `architecture_baseline.json`.

## Goals / Non-Goals

**Goals:**
- Land D1–D4 as additive, backwards-compatible engineering: control dataset contract v2 with per-record provenance, KEAP1 positive manifest and negative scaffold, control scoring script, explicit core calibration scope, formal-store persistence of calibration output, and a read-only soft desirability view.
- Keep `calibrate_thresholds` / `load_control_dataset` call-compatible so existing callers and tests keep working.

**Non-Goals:**
- No P0-E Pareto/tournament; the soft view is read-only and minimal.
- No changes to prediction/scoring algorithms; controls are scored by reusing the existing pipeline.
- No GPU calibration run in this change; actual calibrated values are produced on the server.
- No new hash/SHA256 machinery (AGENTS.md); provenance uses explicit fields.

## Decisions

**D1: Control dataset contract v2 with provenance**
- Introduce `CONTROL_DATASET_VERSION = 2` while keeping v1 files loadable: `validate_control_metadata` accepts both `1` and `2`; new calibration writes emit v2. v2 records require `role` (`positive`/`negative`) and a `source` object with a reference identifier (`pdb_id` and/or `doi`) and method note; records without them are rejected with `ControlDataError`. v1 keeps legacy behavior.
- Add `benchmarks/keap1/calibration/positive_controls.json` (6 KEAP1 positives derived from `runtime_manifest.json`, provenance: PDB + DOI + role) and a `scripts/prepare_keap1_controls.py` generator that emits the full control manifest including deterministic sequence-permutation negatives labelled `in_silico_sequence_negative_control` (source = originating positive PDB + method + permutation basis). No metric values and no `approved_digest`/protocol binding are baked into the shipped manifest.
- Add `scripts/score_control_dataset.py`: reads the control manifest, runs the prediction pipeline to compute core metric values for each control, and emits a bound v2 control dataset (metadata filled from the current approved project config at run time). Locally the script is syntax- and unit-tested; actual scoring runs on the server.
- Rationale: keeps the benchmark asset provenance-only and unbound, so it cannot silently override thresholds; binding happens at scoring/calibration time against the approved config. Alternative (baking binding into the shipped file) was rejected because `approved_digest` is config-specific.

**D2: Explicit core calibration scope**
- Add `CALIBRATION_METRIC_KEYS = {"L2_ipsae", "L4_nc_term_dist", "L5_hotspot_coverage", "L6_pose_rmsd", "L7_scrmsd"}` as the single source of truth in `threshold_calibration.py`, aligned with the KEAP1 doc priority (L2/L5/L6) plus the ring-closure and backbone gates (L4/L7). `calibrate_thresholds` gains an optional `metric_keys` parameter defaulting to this set; non-eligible metrics are skipped with audit reason `not_calibration_eligible` and their thresholds untouched. `METRIC_SPECS` stays as the value-extraction reference for all metrics.
- Rationale: a small explicit set matches v3 D2 ("4–6 decisive metrics") and prevents controls from silently rewriting literature thresholds for metrics that are not calibrated on purpose. Alternative (config-driven list) rejected for now to keep the contract fixed and auditable.

**D3: Formal-store persistence**
- Add `State.register_artifact(...)` in `data_layer.py` delegating to `SQLiteStore.register_artifact`; `_apply_control_calibration` registers `_threshold_calibration.json` as artifact type `threshold_calibration` after a successful calibration, alongside the existing formal `threshold_calibration` evidence event.
- Threshold state keeps flowing through `sync_thresholds_from_cache` → SQLite `replace_state`; the JSON cache files remain projections/compatibility surfaces and are documented as such. No change to execution transaction ownership.
- Rationale: uses the existing artifact/evidence/state boundaries (architectural invariants) without a new table or bypass. Alternative (dedicated `thresholds` table) rejected: it would duplicate the state projection and add migration cost with no behavioral gain now.

**D4: Status semantics and soft desirability view**
- Document canonical `calibration_status` values (`calibrated`, `pending`, `unavailable`, `not_separated`) in `threshold_contract`; `calibrate_thresholds` keeps setting them per metric.
- Add a read-only `soft_desirability(candidate, thresholds, target_ids)` helper (new small module or `battery_evaluation` addition) returning per-metric `{value, desirability, calibration_status, hard_eligible, reason}` where `hard_eligible` mirrors `_threshold_is_justified`. Uncalibrated metrics are soft-only and never alter `competition_clearance`; the existing hard gate stays unchanged (it already denies clearance when a metric is ungraded/uncalibrated).
- Rationale: satisfies D4 with a separate, clearly-labelled surface and zero risk to the existing clearance semantics. Alternative (changing battery result schema) rejected: it would ripple through callers and tests.

## Risks / Trade-offs

- [Risk] Sequence-permutation negatives are in-silico decoys, not experimentally validated non-binders; calibration validity depends on the team confirming the negative set → negatives are explicitly labelled with provenance and method; calibration only replaces thresholds when controls separate under the FPR ceiling; docs call out the limitation.
- [Risk] v2 provenance enforcement could reject legacy datasets → v1 stays loadable and only v2 requires provenance; existing tests keep passing.
- [Risk] Artifact registration adds a Store write in the Research path → additive and non-transactional-change; failure to register is logged as evidence, not fatal.
- [Risk] Control scoring cannot be executed locally (GPU tools) → scoring script is unit-tested for manifest→bound-dataset conversion and syntax-validated; real values are a server-side step documented in the script docstring.

## Migration Plan

- Additive only: new constants, new helper, artifact registration, new benchmark assets, new scripts, new tests. No threshold-entry schema migration.
- Rollback: revert the branch; JSON cache and state paths are unchanged; artifact rows are append-only and harmless.
- Docs: update `docs/keap1_backend_validation_20260803.md` or add a threshold-calibration note describing the v2 contract, core scope, and soft/hard distinction.
