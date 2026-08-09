import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import agents.research as research
import data_layer
from threshold_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    ControlDataError,
    calibrate_threshold,
    calibrate_thresholds,
    load_control_dataset,
)
from scripts.prepare_keap1_controls import build_manifest
from threshold_contract import merge_thresholds


def _control_set(n_negative=20, n_positive=4):
    records = []
    for index in range(n_negative):
        records.append({
            "control_id": f"N{index:02d}",
            "label": "negative",
            "role": "negative_control",
            "source": {"pdb_id": "7K2E", "method": "negative-control"},
            "metrics": {
                "global": {
                    "plddt": 0.20 + index * 0.01,
                    "nc_distance_pre": 3.0 + index * 0.01,
                    "nc_distance_post": 3.1 + index * 0.01,
                    "scrmsd": 3.0 + index * 0.01,
                },
                "targets": {
                    target: {
                        "ipsae": 0.20 + index * 0.01,
                        "dg": -4.0 + index * 0.05,
                        "sc": 0.30 + index * 0.005,
                        "dsasa": 180.0 + index,
                        "hotspot_cov": 0.20 + index * 0.01,
                        "pose_rmsd": 3.0 + index * 0.01,
                    }
                    for target in ("MDM2", "MDMX")
                },
            },
        })
    for index in range(n_positive):
        records.append({
            "control_id": f"P{index:02d}",
            "label": "positive",
            "role": "positive_control",
            "source": {"pdb_id": "7K2E", "doi": "10.1021/jacs.0c09799", "method": "positive-control"},
            "metrics": {
                "global": {
                    "plddt": 0.82 + index * 0.01,
                    "nc_distance_pre": 1.1 + index * 0.02,
                    "nc_distance_post": 1.2 + index * 0.02,
                    "scrmsd": 1.0 + index * 0.02,
                },
                "targets": {
                    target: {
                        "ipsae": 0.78 + index * 0.01,
                        "dg": -14.0 - index * 0.1,
                        "sc": 0.72 + index * 0.01,
                        "dsasa": 520.0 + index,
                        "hotspot_cov": 0.80 + index * 0.01,
                        "pose_rmsd": 1.1 + index * 0.02,
                    }
                    for target in ("MDM2", "MDMX")
                },
            },
        })
    return records


class ThresholdCalibrationTests(unittest.TestCase):
    def test_scalar_result_contains_small_sample_audit(self):
        result = calibrate_threshold(
            metric="ipsae",
            target_id="MDM2",
            negatives=[0.1 + i * 0.02 for i in range(20)],
            positives=[0.75, 0.8, 0.85],
            protocol={"tool": "test", "seed_count": 5},
        )
        self.assertEqual(result["calibration_status"], "calibrated")
        self.assertLessEqual(result["observed_false_positive_rate"], 0.05)
        self.assertEqual(len(result["false_positive_rate_ci95"]), 2)
        self.assertTrue(result["protocol_hash"])

    def test_batch_calibrates_global_and_target_specific_layers(self):
        thresholds, audit = calibrate_thresholds(
            controls=_control_set(),
            thresholds=research.default_thresholds(research.PROJECT_CONFIG),
            target_ids=("MDM2", "MDMX"),
            protocol={"tool": "same-protocol", "version": "test-1"},
        )
        self.assertEqual(audit["schema_version"], CALIBRATION_SCHEMA_VERSION)
        self.assertEqual(audit["status"], "calibrated")
        self.assertEqual(audit["skipped_keys"], [])
        self.assertEqual(
            set(audit["excluded_keys"]),
            {"L1_plddt", "L3_dg", "L3_sc", "L3_dsasa"},
        )
        self.assertEqual(thresholds["L7_scrmsd"]["calibration_status"], "calibrated")
        self.assertEqual(
            thresholds["L2_ipsae"]["targets"]["MDM2"]["evidence_grade"],
            "positive_control",
        )
        self.assertEqual(
            thresholds["L6_pose_rmsd"]["targets"]["MDMX"]["calibration_status"],
            "calibrated",
        )
        self.assertIn("L5_hotspot_coverage:MDM2", audit["calibrated_keys"])

    def test_metric_keys_as_single_string_calibrates_only_that_key(self):
        thresholds, audit = calibrate_thresholds(
            controls=_control_set(),
            thresholds=research.default_thresholds(research.PROJECT_CONFIG),
            target_ids=("MDM2", "MDMX"),
            protocol={"tool": "same-protocol", "version": "test-1"},
            metric_keys="L7_scrmsd",
        )
        self.assertIn("L7_scrmsd", audit["calibrated_keys"])
        self.assertEqual(thresholds["L7_scrmsd"]["calibration_status"], "calibrated")
        self.assertIn("L2_ipsae", audit["excluded_keys"])
        self.assertNotIn("L2_ipsae", audit["calibrated_keys"])

    def test_insufficient_controls_never_replace_provisional_thresholds(self):
        original = research.default_thresholds(research.PROJECT_CONFIG)
        calibrated, audit = calibrate_thresholds(
            controls=_control_set(n_negative=2, n_positive=1),
            thresholds=original,
            target_ids=("MDM2", "MDMX"),
        )
        self.assertEqual(audit["status"], "insufficient_controls")
        self.assertEqual(audit["calibrated_keys"], [])
        self.assertEqual(calibrated, original)

    def test_overlapping_controls_are_not_marked_as_calibrated(self):
        original = {"L7_scrmsd": {"value": 3.0, "evidence_grade": "team_provisional"}}
        records = []
        for index in range(10):
            records.append({"id": f"N{index}", "label": "negative", "metrics": {"global": {"scrmsd": 0.8}}})
        for index in range(3):
            records.append({"id": f"P{index}", "label": "positive", "metrics": {"global": {"scrmsd": 0.8}}})
        calibrated, audit = calibrate_thresholds(
            controls=records,
            thresholds=original,
            min_positive_recall=0.5,
        )
        self.assertEqual(audit["status"], "not_separated")
        self.assertEqual(calibrated, original)
        self.assertEqual(audit["metrics"]["L7_scrmsd"]["status"], "not_separated")

    def test_mixed_control_protocols_are_rejected(self):
        records = [
            {
                "id": "N1", "label": "negative", "protocol": {"tool": "a"},
                "metrics": {"global": {"plddt": 0.2}},
            },
            {
                "id": "N2", "label": "negative", "protocol": {"tool": "b"},
                "metrics": {"global": {"plddt": 0.3}},
            },
        ]
        _, audit = calibrate_thresholds(controls=records)
        self.assertEqual(audit["status"], "invalid_controls")
        self.assertIn("protocol_mismatch", audit)

    def test_dataset_approval_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls.json"
            path.write_text(json.dumps({
                "project_id": "other-project",
                "approved_digest": "old",
                "controls": _control_set(1, 1),
            }), encoding="utf-8")
            with self.assertRaises(ControlDataError):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest="new",
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_unbound_control_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls.json"
            path.write_text(json.dumps({"controls": _control_set(1, 1)}), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "missing required binding"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_control_protocol_must_match_current_scoring_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls.json"
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "wrong", "version": "test-1"},
                },
                "controls": _control_set(1, 1),
            }), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "protocol does not match"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_state_merge_keeps_calibrated_target_overrides(self):
        existing = {
            "L2_ipsae": {
                "value": 0.55,
                "evidence_grade": "team_provisional",
                "targets": {
                    "MDM2": {
                        "value": 0.70,
                        "evidence_grade": "positive_control",
                        "calibration_status": "calibrated",
                    }
                },
            }
        }
        incoming = {
            "L2_ipsae": {
                "value": 0.55,
                "evidence_grade": "team_provisional",
                "targets": {
                    "MDMX": {
                        "value": 0.68,
                        "evidence_grade": "positive_control",
                        "calibration_status": "calibrated",
                    }
                },
            }
        }
        merged, audit = merge_thresholds(existing, incoming)
        self.assertEqual(set(merged["L2_ipsae"]["targets"]), {"MDM2", "MDMX"})
        self.assertIn("L2_ipsae", audit["overwritten"])

    def test_research_optional_control_layer_is_audited_without_breaking_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = root / "data"
            evidence = root / "evidence"
            controls = root / "controls.json"
            controls.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": _control_set(),
            }), encoding="utf-8")
            config = deepcopy(research.PROJECT_CONFIG)
            config["selection"] = {
                **(config.get("selection") or {}),
                "calibration_protocol": {"tool": "same-protocol", "version": "test-1"},
            }
            with patch.dict("os.environ", {"CYCPEP_CONTROL_DATA": str(controls)}, clear=False), \
                 patch.object(research, "DATA_DIR", data), \
                 patch.object(research, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "DATA_DIR", data), \
                 patch.object(data_layer, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "LOG_PATH", evidence / "evidence_log.jsonl"), \
                 patch.object(research, "THRESHOLDS_CACHE", data / "_thresholds_cache.json"):
                result, summary = research._apply_control_calibration(
                    research.default_thresholds(config),
                    config,
                )
            self.assertEqual(summary["status"], "calibrated")
            self.assertTrue((data / "_threshold_calibration.json").is_file())
            self.assertEqual(result["L7_scrmsd"]["calibration_status"], "calibrated")
            events = (evidence / "evidence_log.jsonl").read_text(encoding="utf-8")
            self.assertIn("threshold_calibration", events)

    def test_research_cache_tracks_control_dataset_digest(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls.json"
            path.write_text('{"controls": []}', encoding="utf-8")
            with patch.dict("os.environ", {"CYCPEP_CONTROL_DATA": str(path)}, clear=False):
                before = research._cache_meta(research.PROJECT_CONFIG)
                path.write_text('{"controls": [{"id": "changed"}]}', encoding="utf-8")
                after = research._cache_meta(research.PROJECT_CONFIG)
            self.assertNotEqual(before["control_data_sha256"], after["control_data_sha256"])
            self.assertIn(
                "control_data_sha256",
                research._cache_mismatch_reasons(before, after),
            )


if __name__ == "__main__":
    unittest.main()

class ControlProvenanceAndScopeTests(unittest.TestCase):
    def test_v1_dataset_loads_without_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls_v1.json"
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": 1,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": _control_set(2, 1),
            }), encoding="utf-8")
            controls, metadata = load_control_dataset(
                path,
                project_id=research.PROJECT_CONFIG["project_id"],
                approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                    "approved_digest"
                ),
                protocol={"tool": "same-protocol", "version": "test-1"},
            )
            self.assertEqual(len(controls), 3)
            self.assertEqual(str(metadata["schema_version"]), "1")

    def test_v2_dataset_missing_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls_v2.json"
            records = [dict(item) for item in _control_set(2, 1)]
            for record in records:
                record.pop("source", None)
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": records,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "provenance"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_non_core_metric_never_replaced(self):
        original = {"L1_plddt": {"value": 0.7, "evidence_grade": "paper_explicit"}}
        records = [
            {"id": f"N{i}", "label": "negative", "metrics": {"global": {"plddt": 0.2}}}
            for i in range(10)
        ] + [
            {"id": f"P{i}", "label": "positive", "metrics": {"global": {"plddt": 0.9}}}
            for i in range(4)
        ]
        calibrated, audit = calibrate_thresholds(
            controls=records,
            thresholds=original,
            min_positive_recall=0.5,
        )
        self.assertEqual(calibrated, original)
        self.assertIn("L1_plddt", audit["excluded_keys"])
        self.assertEqual(
            audit["metrics"]["L1_plddt"]["status"],
            "not_calibration_eligible",
        )
        self.assertNotIn("L1_plddt", audit["calibrated_keys"])

    def test_calibration_registers_artifact_in_store(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = root / "data"
            evidence = root / "evidence"
            controls = root / "controls.json"
            controls.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": _control_set(),
            }), encoding="utf-8")
            config = deepcopy(research.PROJECT_CONFIG)
            config["selection"] = {
                **(config.get("selection") or {}),
                "calibration_protocol": {"tool": "same-protocol", "version": "test-1"},
            }
            with patch.dict("os.environ", {"CYCPEP_CONTROL_DATA": str(controls)}, clear=False), \
                 patch.object(research, "DATA_DIR", data), \
                 patch.object(research, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "DATA_DIR", data), \
                 patch.object(data_layer, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "LOG_PATH", evidence / "evidence_log.jsonl"), \
                 patch.object(research, "THRESHOLDS_CACHE", data / "_thresholds_cache.json"):
                result, summary = research._apply_control_calibration(
                    research.default_thresholds(config),
                    config,
                )
            self.assertEqual(summary["status"], "calibrated")
            db_path = data / "store.db"
            self.assertTrue(db_path.is_file())
            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT artifact_id, artifact_type, path FROM artifacts"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "threshold_calibration")
            self.assertTrue(rows[0][2].endswith("_threshold_calibration.json"))
            events = (evidence / "evidence_log.jsonl").read_text(encoding="utf-8")
            self.assertIn("threshold_calibration", events)

class ControlManifestTests(unittest.TestCase):
    def test_manifest_generation_is_deterministic(self):
        root = Path(__file__).resolve().parent
        positives = root / "benchmarks" / "keap1" / "calibration" / "positive_controls.json"
        if not positives.is_file():
            self.skipTest("KEAP1 positive controls manifest not present")
        first = build_manifest(positives, negatives_per_positive=3, seed=20260809)
        second = build_manifest(positives, negatives_per_positive=3, seed=20260809)
        self.assertEqual(first, second)
        labels = [item["label"] for item in first["controls"]]
        self.assertEqual(labels.count("positive"), 6)
        self.assertEqual(labels.count("negative"), 18)
        self.assertTrue(
            all(item["role"] for item in first["controls"] if item["label"] == "negative")
        )

    def test_v2_dataset_missing_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls_v2_norole.json"
            records = [dict(item) for item in _control_set(2, 1)]
            for record in records:
                record.pop("label", None)
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": records,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "label positive/negative"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_v2_dataset_missing_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls_v2_norole.json"
            records = [dict(item) for item in _control_set(2, 1)]
            for record in records:
                record.pop("role", None)
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": records,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "requires per-record role"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_unsupported_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "controls_v3.json"
            path.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": 3,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": _control_set(2, 1),
            }), encoding="utf-8")
            with self.assertRaisesRegex(ControlDataError, "schema_version is not supported"):
                load_control_dataset(
                    path,
                    project_id=research.PROJECT_CONFIG["project_id"],
                    approved_digest=(research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    protocol={"tool": "same-protocol", "version": "test-1"},
                )

    def test_calibration_artifact_registration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = root / "data"
            evidence = root / "evidence"
            controls = root / "controls.json"
            controls.write_text(json.dumps({
                "metadata": {
                    "project_id": research.PROJECT_CONFIG["project_id"],
                    "approved_digest": (research.PROJECT_CONFIG.get("review") or {}).get(
                        "approved_digest"
                    ),
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "protocol": {"tool": "same-protocol", "version": "test-1"},
                },
                "controls": _control_set(),
            }), encoding="utf-8")
            config = deepcopy(research.PROJECT_CONFIG)
            config["selection"] = {
                **(config.get("selection") or {}),
                "calibration_protocol": {"tool": "same-protocol", "version": "test-1"},
            }
            with patch.dict("os.environ", {"CYCPEP_CONTROL_DATA": str(controls)}, clear=False), \
                 patch.object(research, "DATA_DIR", data), \
                 patch.object(research, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "DATA_DIR", data), \
                 patch.object(data_layer, "EVIDENCE_DIR", evidence), \
                 patch.object(data_layer, "LOG_PATH", evidence / "evidence_log.jsonl"), \
                 patch.object(research, "THRESHOLDS_CACHE", data / "_thresholds_cache.json"):
                research._apply_control_calibration(
                    research.default_thresholds(config), config,
                )
                research._apply_control_calibration(
                    research.default_thresholds(config), config,
                )
            connection = sqlite3.connect(data / "store.db")
            try:
                rows = connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE artifact_type='threshold_calibration'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(len(rows), 1)

