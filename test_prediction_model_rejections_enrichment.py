"""Public enrichment regressions for typed Rosetta model rejection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _prediction_test_utils import SEQUENCE, atom_line
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.contracts import ContractError
from prediction_pipeline.rosetta_worker import run_rosetta_interface as real_rosetta
from scripts.enrich_prediction_evidence import _enrich_target, run


def _write_complex_with_terminal_distance(path: Path, distance: float) -> None:
    lines: list[str] = []
    serial = 1
    for residue_number in range(1, 4):
        for atom, x in (("N", 20.0 + residue_number), ("CA", 21.0 + residue_number), ("C", 22.0 + residue_number)):
            lines.append(atom_line(serial, atom, "ALA", "A", residue_number, (x, 0.0, 0.0)))
            serial += 1
    one_to_three = {
        "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU",
        "F": "PHE", "G": "GLY", "H": "HIS", "I": "ILE",
    }
    for residue_number, amino_acid in enumerate(SEQUENCE, 1):
        first_n = 0.0 if residue_number == 1 else float(residue_number * 3)
        last_c = distance if residue_number == len(SEQUENCE) else first_n + 2.0
        for atom, x in (("N", first_n), ("CA", first_n + 1.0), ("C", last_c)):
            lines.append(atom_line(
                serial, atom, one_to_three[amino_acid], "B", residue_number,
                (x, 5.0, 0.0),
            ))
            serial += 1
    path.write_text("".join(lines) + "END\n", encoding="utf-8")


class PredictionModelRejectionEnrichmentTests(unittest.TestCase):
    @staticmethod
    def _args(fake_python: Path) -> SimpleNamespace:
        return SimpleNamespace(
            rosetta_scripts=None,
            pyrosetta_python=str(fake_python),
            rosetta_timeout=30,
            seed=101,
            post_relax_seed=20260802,
            post_relax_repeats=3,
        )

    def test_real_open_geometry_becomes_bound_rejection_and_remaining_models_continue(self):
        root = Path(tempfile.mkdtemp(prefix="prediction-rejection-enrichment-"))
        fake_python = root / "python"
        fake_python.write_text("", encoding="utf-8")
        predictions = []
        observed = (1.295, 2.570, 1.615)
        for model_index, distance in enumerate(observed):
            pdb = root / f"model_{model_index}.pdb"
            metadata = root / f"model_{model_index}.json"
            _write_complex_with_terminal_distance(pdb, distance)
            metadata.write_text(json.dumps({
                "model_id": f"alphafold2_model_{model_index}",
                "binder_chain": "B",
            }), encoding="utf-8")
            predictions.append({
                "predictor": "ColabDesign",
                "model_id": f"alphafold2_model_{model_index}",
                "seed": model_index,
                "pdb": str(pdb),
                "metadata": str(metadata),
                "binder_chain": "B",
            })

        successful_models: list[str] = []

        def scientific_seam(**kwargs):
            if kwargs["model_id"] == "alphafold2_model_1":
                return real_rosetta(**kwargs)
            successful_models.append(kwargs["model_id"])
            return {
                "predictor": kwargs["predictor"],
                "model_id": kwargs["model_id"],
                "seed": kwargs["seed"],
                "prediction_pdb_sha256": file_sha256(kwargs["complex_pdb"]),
                "output": str(root / f"{kwargs['model_id']}.sc"),
                "output_sha256": "a" * 64,
                "metadata": str(root / f"{kwargs['model_id']}-rosetta.json"),
                "metadata_sha256": "b" * 64,
            }

        bundle = {"targets": {"MDM2": {
            "target_chain": "A",
            "complex_predictions": predictions,
        }}}
        args = self._args(fake_python)
        args.output_root = str(root / "output")
        candidate = SimpleNamespace(sequence=SEQUENCE)

        with (
            patch(
                "scripts.enrich_prediction_evidence._target_coordinates",
                return_value=(root / "target.pdb", "A", "AAA"),
            ),
            patch(
                "scripts.enrich_prediction_evidence.run_rosetta_interface",
                side_effect=scientific_seam,
            ),
            patch(
                "scripts.enrich_prediction_evidence._validate_enrichment_options",
                return_value=(False, True, False),
            ),
            patch(
                "scripts.enrich_prediction_evidence._load_source_context",
                return_value=(
                    bundle, root / "artifacts.json",
                    SimpleNamespace(candidate_id="C0001", sequence=SEQUENCE),
                    {"targets": [{"id": "MDM2"}]}, ["MDM2"],
                ),
            ),
            patch("scripts.enrich_prediction_evidence.validate_bundle_protocol"),
            patch("scripts.enrich_prediction_evidence.load_artifact_bundle") as loader,
        ):
            loader.return_value = SimpleNamespace(sha256="a" * 64, digest="b" * 64)
            result = run(args)

        target = json.loads(Path(result["output_bundle"]).read_text())["targets"]["MDM2"]
        self.assertEqual(successful_models, [
            "alphafold2_model_0", "alphafold2_model_2",
        ])
        self.assertEqual(
            [item["model_id"] for item in target["rosetta_outputs"]],
            ["alphafold2_model_0", "alphafold2_model_2"],
        )
        self.assertEqual(len(target["rosetta_rejections"]), 1)
        rejection = target["rosetta_rejections"][0]
        self.assertEqual(rejection["code"], "rosetta_cyclic_bond_open")
        self.assertEqual(rejection["model_id"], "alphafold2_model_1")
        self.assertAlmostEqual(
            rejection["observed_terminal_c_to_n_distance_angstrom"], 2.570, places=3,
        )
        self.assertEqual(
            rejection["prediction_pdb_sha256"], file_sha256(predictions[1]["pdb"]),
        )

    def test_non_whitelisted_rosetta_failure_remains_task_fatal(self):
        root = Path(tempfile.mkdtemp(prefix="prediction-rejection-fatal-"))
        fake_python = root / "python"
        fake_python.write_text("", encoding="utf-8")
        pdb = root / "model.pdb"
        metadata = root / "model.json"
        _write_complex_with_terminal_distance(pdb, 1.295)
        metadata.write_text(json.dumps({
            "model_id": "alphafold2_model_0", "binder_chain": "B",
        }), encoding="utf-8")
        bundle = {"targets": {"MDM2": {
            "target_chain": "A",
            "complex_predictions": [{
                "predictor": "ColabDesign", "seed": 0,
                "pdb": str(pdb), "metadata": str(metadata), "binder_chain": "B",
            }],
        }}}

        with (
            patch(
                "scripts.enrich_prediction_evidence._target_coordinates",
                return_value=(root / "target.pdb", "A", "AAA"),
            ),
            patch(
                "scripts.enrich_prediction_evidence.run_rosetta_interface",
                side_effect=ContractError("rosetta_failed", "injected later tool failure"),
            ),
            self.assertRaises(ContractError) as raised,
        ):
            _enrich_target(
                self._args(fake_python), "MDM2", {"MDM2": {}},
                SimpleNamespace(sequence=SEQUENCE), bundle,
                root / "output", root / "artifacts.json",
                add_boltz=False, add_rosetta=True,
            )

        self.assertEqual(raised.exception.code, "rosetta_failed")
        self.assertNotIn("rosetta_outputs", bundle["targets"]["MDM2"])
        self.assertNotIn("rosetta_rejections", bundle["targets"]["MDM2"])


if __name__ == "__main__":
    unittest.main()
