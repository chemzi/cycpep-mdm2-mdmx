"""Focused evaluation regressions for typed Rosetta model rejections."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from data_layer import CandidateIndex, EvidenceLogger
from battery_evaluation import evaluate_battery
from prediction_pipeline.target_physics import parse_target_physics
from prediction_pipeline.contracts import (
    SCHEMA_VERSION as ARTIFACT_SCHEMA_VERSION,
    CRITIC_READY_STATUSES,
    prediction_status_from_battery,
)
from prediction_pipeline.protocol import PREDICTION_PROTOCOL

from _prediction_test_utils import justified_thresholds
import test_prediction_pipeline as pipeline_test_fixtures


IDENTITY_HASHES = ("1" * 64, "2" * 64, "3" * 64)
ROSETTA_MAX_DISTANCE = float(
    PREDICTION_PROTOCOL["parameters"]["rosetta_interface"]
    ["maximum_terminal_c_to_n_distance_angstrom"]
)


def _identity(index: int) -> dict:
    return {
        "predictor": "ColabDesign",
        "model_id": f"alphafold2_model_{index + 1}",
        "seed": index,
        "prediction_pdb_sha256": IDENTITY_HASHES[index],
    }


def _rejection(index: int, distance: float) -> dict:
    return {
        **_identity(index),
        "target_chain": "A",
        "binder_chain": "B",
        "binder_sequence": "ACDEFGHI",
        "code": "rosetta_cyclic_bond_open",
        "observed_terminal_c_to_n_distance_angstrom": distance,
        "maximum_terminal_c_to_n_distance_angstrom": ROSETTA_MAX_DISTANCE,
    }


class PredictionModelRejectionMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="prediction-rejection-metrics-"))

    def _output(self, name: str, content: str) -> dict:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return {"path": path, "sha256": name.ljust(64, "0")[:64]}

    def _prodigy(self, index: int, dg: float) -> dict:
        return {
            **_identity(index),
            "output": self._output(f"prodigy-{index}.txt", f"{dg}\n"),
        }

    def _rosetta(self, index: int, dsasa: float, sc: float) -> dict:
        return {
            **_identity(index),
            "output": self._output(
                f"rosetta-{index}.sc",
                "SCORE: dSASA_int sc_value description\n"
                f"SCORE: {dsasa} {sc} model_{index}\n",
            ),
        }

    def test_mixed_cohort_uses_same_rosetta_eligible_models_for_all_l3_metrics(self):
        physics, provenance = parse_target_physics({
            "prodigy_outputs": [
                self._prodigy(0, -6.0),
                self._prodigy(1, -30.0),
                self._prodigy(2, -10.0),
            ],
            "rosetta_outputs": [
                self._rosetta(0, 400.0, 0.6),
                self._rosetta(2, 600.0, 0.8),
            ],
            "rosetta_rejections": [_rejection(1, 2.570)],
        })

        self.assertEqual(physics["dg"], -8.0)
        self.assertEqual(physics["dsasa"], 500.0)
        self.assertAlmostEqual(physics["sc"], 0.7)
        self.assertEqual(
            physics["rosetta_scientific_rejections"],
            [_rejection(1, 2.570)],
        )
        prodigy = next(
            item for item in provenance if item.get("tool") == "PRODIGY"
        )
        self.assertEqual(
            [sample["model_id"] for sample in prodigy["samples"]],
            ["alphafold2_model_1", "alphafold2_model_3"],
        )
        self.assertEqual(
            [sample["model_id"] for sample in prodigy["diagnostic_samples"]],
            ["alphafold2_model_2"],
        )

    def test_all_rejected_keeps_diagnostics_without_numeric_l3_aggregates(self):
        physics, provenance = parse_target_physics({
            "prodigy_outputs": [
                self._prodigy(0, -6.0),
                self._prodigy(1, -30.0),
                self._prodigy(2, -10.0),
            ],
            "rosetta_outputs": [],
            "rosetta_rejections": [
                _rejection(0, 2.1),
                _rejection(1, 2.570),
                _rejection(2, 2.2),
            ],
        })

        self.assertNotIn("dg", physics)
        self.assertNotIn("sc", physics)
        self.assertNotIn("dsasa", physics)
        self.assertEqual(len(physics["rosetta_scientific_rejections"]), 3)
        prodigy = next(
            item for item in provenance if item.get("tool") == "PRODIGY"
        )
        self.assertEqual(prodigy["samples"], [])
        self.assertEqual(len(prodigy["diagnostic_samples"]), 3)

    def test_mixed_rejection_is_complete_negative_l3_evidence(self):
        outcome = evaluate_battery(
            _complete_candidate(
                dg=-8.0,
                sc=0.7,
                dsasa=500.0,
                rejections=[_rejection(1, 2.570)],
            ),
            justified_thresholds(),
            required_targets=("MDM2",),
        )

        self.assertFalse(outcome["l3_pass"])
        self.assertFalse(outcome["target_pass"]["MDM2"]["l3_pass"])
        self.assertIn("l3_pass", outcome["failed_layers"])
        self.assertEqual(outcome["missing_evidence"], [])
        self.assertEqual(outcome["triage_status"], "needs_optimization")
        self.assertEqual(
            prediction_status_from_battery(outcome),
            "needs_optimization",
        )

    def test_all_rejected_is_failed_not_missing_and_has_no_fake_l3_values(self):
        candidate = _complete_candidate(
            rejections=[
                _rejection(0, 2.1),
                _rejection(1, 2.570),
                _rejection(2, 2.2),
            ]
        )
        outcome = evaluate_battery(
            candidate,
            justified_thresholds(),
            required_targets=("MDM2",),
        )

        self.assertFalse(outcome["l3_pass"])
        self.assertIn("l3_pass", outcome["failed_layers"])
        self.assertEqual(outcome["missing_evidence"], [])
        self.assertIsNone(outcome["layer_values"]["L3_dg_mdm2"])
        self.assertIsNone(outcome["layer_values"]["L3_sc_mdm2"])
        self.assertIsNone(outcome["layer_values"]["L3_dsasa_mdm2"])
        self.assertEqual(outcome["triage_status"], "needs_optimization")
        self.assertEqual(
            prediction_status_from_battery(outcome),
            "needs_optimization",
        )

    def test_pipeline_ingests_complete_mixed_negative_as_critic_ready_status(self):
        fixture = pipeline_test_fixtures.PredictionPipelineTests(
            methodName="test_complete_evidence_finalizes_and_resume_is_cached"
        )
        fixture.setUp()
        try:
            reference = fixture._register_candidate()
            fixture._write_complete_artifacts(reference)
            _install_mixed_mdm2_bundle(fixture.artifacts_root / "C0001")

            summary = fixture._pipeline(thresholds=justified_thresholds()).run()

            self.assertEqual(summary["status_counts"], {"needs_optimization": 1})
            row = CandidateIndex.find("C0001")
            self.assertEqual(row["final_status"], "needs_optimization")
            record = json.loads(
                (fixture.run_root / "test_run" / "records" / "C0001.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(record["status"], "needs_optimization")
            self.assertIn("needs_optimization", CRITIC_READY_STATUSES)
            self.assertFalse(record["battery"]["l3_pass"])
            self.assertIn("l3_pass", record["battery"]["failed_layers"])
            self.assertEqual(record["battery"]["missing_evidence"], [])
            self.assertNotIn(
                "l3_physics_missing",
                {issue["code"] for issue in record["issues"]},
            )
            self.assertEqual(record["metrics"]["targets"]["MDM2"]["dg"], -8.0)
            self.assertTrue(any(
                event.get("event_type") == "battery_evaluated"
                and event.get("candidate_id") == "C0001"
                for event in EvidenceLogger.get_all()
            ))
        finally:
            fixture.tearDown()


def _complete_candidate(
    *,
    dg: float | None = None,
    sc: float | None = None,
    dsasa: float | None = None,
    rejections: list[dict],
) -> dict:
    target = {
        "ipsae": 0.9,
        "hotspot_cov": 1.0,
        "site_consistency": True,
        "pose_rmsd": 0.5,
        "seed_convergence": 1.0,
        "rosetta_scientific_rejections": rejections,
    }
    if dg is not None:
        target["dg"] = dg
    if sc is not None:
        target["sc"] = sc
    if dsasa is not None:
        target["dsasa"] = dsasa
    return {
        "metrics": {
            "global": {
                "plddt": 0.95,
                "nc_distance_pre": 1.3,
                "nc_distance_post": 1.3,
                "scrmsd": 0.5,
            },
            "targets": {"MDM2": target},
        },
        "dg_method": "prodigy",
    }


def _install_mixed_mdm2_bundle(candidate_dir: Path) -> None:
    bundle_path = candidate_dir / "artifacts.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["schema_version"] = ARTIFACT_SCHEMA_VERSION
    target = bundle["targets"]["MDM2"]
    target.pop("prodigy_output")
    target.pop("rosetta_outputs")
    prodigy_outputs = []
    rosetta_outputs = []
    rejection = None
    prodigy_values = (-6.0, -30.0, -10.0)
    rosetta_values = {
        0: (400.0, 0.6),
        2: (600.0, 0.8),
    }
    for index, prediction in enumerate(target["complex_predictions"]):
        metadata = json.loads(
            (candidate_dir / prediction["metadata"]).read_text(encoding="utf-8")
        )
        pdb_path = candidate_dir / prediction["pdb"]
        prediction_hash = hashlib.sha256(pdb_path.read_bytes()).hexdigest()
        identity = {
            "predictor": prediction["predictor"],
            "model_id": metadata["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": prediction_hash,
        }
        prodigy_path = candidate_dir / f"MDM2_{index}_mixed_prodigy.txt"
        prodigy_path.write_text(f"{prodigy_values[index]}\n", encoding="utf-8")
        prodigy_outputs.append({**identity, "output": prodigy_path.name})
        if index == 1:
            rejection = {
                **identity,
                "target_chain": "A",
                "binder_chain": "B",
                "binder_sequence": "ACDEFGHI",
                "code": "rosetta_cyclic_bond_open",
                "observed_terminal_c_to_n_distance_angstrom": 2.570,
                "maximum_terminal_c_to_n_distance_angstrom": ROSETTA_MAX_DISTANCE,
            }
            continue
        dsasa, sc = rosetta_values[index]
        score_path = candidate_dir / f"MDM2_{index}_mixed_rosetta.sc"
        score_path.write_text(
            "SCORE: dSASA_int sc_value description\n"
            f"SCORE: {dsasa} {sc} model_{index}\n",
            encoding="utf-8",
        )
        score_metadata_path = candidate_dir / f"MDM2_{index}_mixed_rosetta.json"
        score_metadata_path.write_text(json.dumps({
            "tool": "PyRosetta InterfaceAnalyzerMover",
            "tool_version_output": "test",
            "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
            **identity,
            "target_chain": "A",
            "binder_chain": "B",
            "binder_sequence": "ACDEFGHI",
            "terminal_c_to_n_distance_angstrom": 1.3,
            "declared_bond": {
                "res1": 10,
                "atom1": "C",
                "res2": 3,
                "atom2": "N",
            },
            "scorefunction": "ref2015",
            "metrics": {"dsasa": dsasa, "sc": sc},
            "xml_sha256": "a" * 64,
        }), encoding="utf-8")
        rosetta_outputs.append({
            **identity,
            "output": score_path.name,
            "metadata": score_metadata_path.name,
        })
    target["prodigy_outputs"] = prodigy_outputs
    target["rosetta_outputs"] = rosetta_outputs
    target["rosetta_rejections"] = [rejection]
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
