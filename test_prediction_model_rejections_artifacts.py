from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import ContractError, SCHEMA_VERSION


SEQUENCE = "ACDEFGHIK"
TARGET_ID = "MDM2"


class PredictionModelRejectionArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="prediction-model-rejection-artifacts-"
        )
        self.root = Path(self.tmp.name)
        self.predictions = [self._prediction(index) for index in range(3)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _prediction(self, index: int) -> dict:
        pdb = self.root / f"complex_{index}.pdb"
        metadata = self.root / f"complex_{index}.json"
        pdb.write_text(f"MODEL {index + 1}\nEND\n", encoding="utf-8")
        metadata.write_text(
            json.dumps(
                {
                    "tool": "ColabDesign",
                    "model_id": f"alphafold2_model_{index + 1}",
                    "seed": 100 + index,
                    "binder_chain": "B",
                }
            ),
            encoding="utf-8",
        )
        return {
            "predictor": "ColabDesign",
            "model_id": f"alphafold2_model_{index + 1}",
            "seed": 100 + index,
            "pdb": pdb.name,
            "pdb_sha256": self._sha256(pdb),
            "metadata": metadata.name,
            "metadata_sha256": self._sha256(metadata),
            "binder_chain": "B",
        }

    def _declared_prediction(self, prediction: dict) -> dict:
        return {
            key: value
            for key, value in prediction.items()
            if key != "model_id"
        }

    def _output(self, prediction: dict) -> dict:
        model_id = prediction["model_id"]
        output = self.root / f"{model_id}.sc"
        metadata = self.root / f"{model_id}_rosetta.json"
        output.write_text(
            "SCORE: dSASA_int sc_value dG_separated description\n"
            f"SCORE: 400 0.6 -8 {model_id}\n",
            encoding="utf-8",
        )
        prediction_sha = prediction["pdb_sha256"]
        metadata.write_text(
            json.dumps(
                {
                    "tool": "PyRosetta InterfaceAnalyzerMover",
                    "tool_version_output": "PyRosetta-4 2026.29",
                    "protocol": (
                        "declare_head_to_tail_then_interface_analyzer_ref2015"
                    ),
                    "predictor": prediction["predictor"],
                    "model_id": model_id,
                    "seed": prediction["seed"],
                    "prediction_pdb_sha256": prediction_sha,
                    "target_chain": "A",
                    "binder_chain": "B",
                    "binder_sequence": SEQUENCE,
                    "terminal_c_to_n_distance_angstrom": 1.3,
                    "declared_bond": {
                        "res1": 11,
                        "atom1": "C",
                        "res2": 3,
                        "atom2": "N",
                    },
                    "scorefunction": "ref2015",
                    "metrics": {
                        "dsasa": 400.0,
                        "sc": 0.6,
                        "rosetta_dg_separated": -8.0,
                    },
                    "xml_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        return {
            "predictor": prediction["predictor"],
            "model_id": model_id,
            "seed": prediction["seed"],
            "prediction_pdb_sha256": prediction_sha,
            "output": output.name,
            "output_sha256": self._sha256(output),
            "metadata": metadata.name,
            "metadata_sha256": self._sha256(metadata),
        }

    def _prodigy_output(self, prediction: dict) -> dict:
        output = self.root / f"{prediction['model_id']}_prodigy.txt"
        output.write_text("-8.0\n", encoding="utf-8")
        return {
            "predictor": prediction["predictor"],
            "model_id": prediction["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": prediction["pdb_sha256"],
            "output": output.name,
            "output_sha256": self._sha256(output),
        }

    @staticmethod
    def _rejection(prediction: dict) -> dict:
        return {
            "predictor": prediction["predictor"],
            "model_id": prediction["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": prediction["pdb_sha256"],
            "target_chain": "A",
            "binder_chain": "B",
            "binder_sequence": SEQUENCE,
            "code": "rosetta_cyclic_bond_open",
            "observed_terminal_c_to_n_distance_angstrom": 2.57,
            "maximum_terminal_c_to_n_distance_angstrom": 2.0,
        }

    def _load(self, target: dict):
        path = self.root / "artifacts.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "candidate_id": "C0001",
                    "sequence": SEQUENCE,
                    "global": {},
                    "targets": {TARGET_ID: target},
                }
            ),
            encoding="utf-8",
        )
        return load_artifact_bundle(
            path,
            candidate_id="C0001",
            sequence=SEQUENCE,
            required_targets=(TARGET_ID,),
        )

    def _target(self) -> dict:
        return {
            "target_chain": "A",
            "complex_predictions": [
                self._declared_prediction(prediction)
                for prediction in self.predictions
            ],
            "prodigy_outputs": [
                self._prodigy_output(prediction) for prediction in self.predictions
            ],
        }

    def test_mixed_outputs_and_rejections_have_exact_exclusive_coverage(self):
        target = self._target()
        target["rosetta_outputs"] = [
            self._output(self.predictions[0]),
            self._output(self.predictions[2]),
        ]
        target["rosetta_rejections"] = [self._rejection(self.predictions[1])]

        loaded = self._load(target).target_artifacts[TARGET_ID]

        self.assertEqual(len(loaded["rosetta_outputs"]), 2)
        self.assertEqual(
            loaded["rosetta_rejections"],
            [self._rejection(self.predictions[1])],
        )
        self.assertNotIn("output", loaded["rosetta_rejections"][0])

    def test_all_models_may_be_covered_by_rejections(self):
        target = self._target()
        target["rosetta_outputs"] = []
        target["rosetta_rejections"] = [
            self._rejection(prediction) for prediction in self.predictions
        ]

        loaded = self._load(target).target_artifacts[TARGET_ID]

        self.assertEqual(loaded["rosetta_outputs"], [])
        self.assertEqual(len(loaded["rosetta_rejections"]), 3)

    def test_missing_duplicate_overlap_and_unbound_coverage_fail_closed(self):
        cases = {}

        missing = self._target()
        missing["rosetta_rejections"] = [self._rejection(self.predictions[0])]
        cases["missing"] = (missing, "rosetta_coverage_mismatch")

        duplicate = self._target()
        rejection = self._rejection(self.predictions[0])
        duplicate["rosetta_rejections"] = [rejection, dict(rejection)]
        cases["duplicate"] = (duplicate, "rosetta_coverage_mismatch")

        overlap = self._target()
        overlap["rosetta_outputs"] = [self._output(self.predictions[0])]
        overlap["rosetta_rejections"] = [
            self._rejection(prediction) for prediction in self.predictions
        ]
        cases["overlap"] = (overlap, "rosetta_coverage_mismatch")

        unbound = self._target()
        invalid = self._rejection(self.predictions[0])
        invalid["prediction_pdb_sha256"] = "f" * 64
        unbound["rosetta_rejections"] = [invalid]
        cases["unbound"] = (unbound, "rosetta_rejection_prediction_mismatch")

        for name, (target, expected_code) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ContractError) as ctx:
                    self._load(target)
                self.assertEqual(ctx.exception.code, expected_code)

    def test_rejection_identity_and_scientific_geometry_are_bound(self):
        valid = self._rejection(self.predictions[0])
        mutations = {
            "predictor": {"predictor": "Boltz"},
            "model_id": {"model_id": "wrong-model"},
            "seed": {"seed": 999},
            "target_chain": {"target_chain": "Z"},
            "binder_chain": {"binder_chain": "Z"},
            "binder_sequence": {"binder_sequence": SEQUENCE[::-1]},
            "code": {"code": "tool_failed"},
            "non_finite": {
                "observed_terminal_c_to_n_distance_angstrom": math.inf
            },
            "not_over_limit": {
                "observed_terminal_c_to_n_distance_angstrom": 2.0
            },
            "wrong_limit": {
                "maximum_terminal_c_to_n_distance_angstrom": 2.1
            },
        }
        for name, mutation in mutations.items():
            target = self._target()
            entries = [self._rejection(prediction) for prediction in self.predictions]
            entries[0] = {**valid, **mutation}
            target["rosetta_rejections"] = entries
            with self.subTest(name=name):
                with self.assertRaises(ContractError):
                    self._load(target)

    def test_rejections_require_identity_bound_per_model_prodigy_outputs(self):
        rejection = self._rejection(self.predictions[0])
        for name, legacy in (("missing", False), ("legacy", True)):
            target = self._target()
            target.pop("prodigy_outputs")
            if legacy:
                output = self.root / "legacy_prodigy.txt"
                output.write_text("-8.0\n", encoding="utf-8")
                target["prodigy_output"] = output.name
            target["rosetta_rejections"] = [
                rejection,
                self._rejection(self.predictions[1]),
                self._rejection(self.predictions[2]),
            ]
            with self.subTest(name=name):
                with self.assertRaises(ContractError) as ctx:
                    self._load(target)
                self.assertEqual(ctx.exception.code, "prodigy_coverage_mismatch")

    def test_legacy_prodigy_remains_valid_without_rejections(self):
        target = self._target()
        target.pop("prodigy_outputs")
        output = self.root / "legacy_prodigy.txt"
        output.write_text("-8.0\n", encoding="utf-8")
        target["prodigy_output"] = output.name

        loaded = self._load(target).target_artifacts[TARGET_ID]

        self.assertEqual(loaded["prodigy_output"]["path"], output)


if __name__ == "__main__":
    unittest.main()
