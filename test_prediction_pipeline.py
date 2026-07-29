"""Production Prediction unit/integration tests; no GPU or external tools required."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import data_layer
from data_layer import CandidateIndex
from prediction_pipeline.contracts import ContractError, PredictionConfig
from prediction_pipeline.colabdesign_worker import (
    _assert_cyclic_offset_supported,
    _cyclic_offset,
)
from prediction_pipeline.metrics import (
    calculate_ipsae,
    parse_prodigy_output,
    parse_rosetta_interface_output,
)
from prediction_pipeline.pipeline import PredictionPipeline
from prediction_pipeline.structures import (
    apply_transform,
    exact_sequence_chain,
    kabsch_transform,
    mean_plddt,
    parse_pdb,
    rmsd,
    terminal_bond_distance,
)


ONE_TO_THREE = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}
SEQUENCE = "ACDEFGHI"


def atom_line(serial, atom, residue, chain, number, xyz, bfactor=90.0):
    element = atom[0]
    return (
        f"ATOM  {serial:5d} {atom:^4s} {residue:>3s} {chain:1s}{number:4d}    "
        f"{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}"
        f"{1.00:6.2f}{bfactor:6.2f}          {element:>2s}\n"
    )


def chain_pdb(sequence, chain, *, shift=(0.0, 0.0, 0.0), bfactor=90.0, start=1):
    lines, serial = [], start
    for index, amino_acid in enumerate(sequence, 1):
        base = np.array([index * 1.2, 0.0, 0.0]) + np.asarray(shift)
        for atom, delta in (
            ("N", (-0.4, 0, 0)),
            ("CA", (0, 0, 0)),
            ("C", (0.4, 0, 0)),
            ("CB", (0, 0.8, 0)),
        ):
            lines.append(atom_line(
                serial,
                atom,
                ONE_TO_THREE[amino_acid],
                chain,
                index,
                base + np.asarray(delta),
                bfactor,
            ))
            serial += 1
    return "".join(lines), serial


def write_monomer(path: Path, sequence=SEQUENCE, bfactor=90.0):
    content, _ = chain_pdb(sequence, "B", bfactor=bfactor)
    path.write_text(content + "END\n", encoding="utf-8")


def write_complex(path: Path, sequence=SEQUENCE, binder_shift=(0, 1.5, 0)):
    target, serial = chain_pdb("AAA", "A", bfactor=95.0)
    binder, _ = chain_pdb(
        sequence, "B", shift=binder_shift, bfactor=90.0, start=serial
    )
    path.write_text(target + binder + "END\n", encoding="utf-8")


def project_config(targets=("MDM2", "MDMX")):
    residues = {"MDM2": [1, 2, 3], "MDMX": [1, 2, 3]}
    project = {
        "schema_version": 1,
        "project_id": "prediction_test",
        "targets": [
            {
                "id": target,
                "required": True,
                "structure": {"pdb_id": "TEST", "chain": "A"},
                "binding_site": {"residues": residues[target], "status": "user_reviewed"},
            }
            for target in targets
        ],
    }
    encoded = json.dumps(
        project, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    project["review"] = {
        "status": "approved",
        "approved_digest": hashlib.sha256(encoded).hexdigest(),
    }
    return project


def justified_thresholds():
    source = {
        "source": "unit-test positive control",
        "evidence_grade": "positive_control",
    }
    return {
        "L1_plddt": {"value": 0.8, "operator": ">", **source},
        "L2_ipsae": {"value": 0.5, "operator": ">", **source},
        "L3_dg": {"value": -5.0, "operator": "<", "method": "prodigy", **source},
        "L3_sc": {"value": 0.5, "operator": ">", **source},
        "L3_dsasa": {"value": 100, "operator": ">", **source},
        "L4_nc_term_dist": {"value": 100, "operator": "<", **source},
        "L5_hotspot_coverage": {"value": 0.67, "operator": ">=", **source},
        "L6_pose_rmsd": {
            "value": 2.0,
            "operator": "<",
            "min_seed_fraction": 0.67,
            **source,
        },
        "L7_scrmsd": {"value": 2.0, "operator": "<", **source},
    }


class StructureAndParserTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prediction-structure-test-"))

    def test_first_model_only_and_plddt_scale_normalization(self):
        first, _ = chain_pdb(SEQUENCE, "B", bfactor=90.0)
        second, _ = chain_pdb("AAAAAAAA", "B", bfactor=10.0)
        path = self.root / "models.pdb"
        path.write_text(
            f"MODEL        1\n{first}ENDMDL\nMODEL        2\n{second}ENDMDL\nEND\n",
            encoding="utf-8",
        )
        structure = parse_pdb(path)
        self.assertEqual(exact_sequence_chain(structure, SEQUENCE), "B")
        value, scale = mean_plddt(structure, "B")
        self.assertAlmostEqual(value, 0.9)
        self.assertEqual(scale, "0-100")

        normalized = self.root / "normalized.pdb"
        write_monomer(normalized, bfactor=0.91)
        value, scale = mean_plddt(parse_pdb(normalized), "B")
        self.assertAlmostEqual(value, 0.91)
        self.assertEqual(scale, "0-1")

    def test_cyclic_offset_closes_terminal_relative_position(self):
        offset = _cyclic_offset(8)
        self.assertEqual(abs(offset[0, 7]), 1)
        self.assertEqual(abs(offset[0, 1]), 1)
        np.testing.assert_array_equal(offset, -offset.T)

    def test_worker_rejects_backend_that_ignores_pairwise_offset(self):
        repository = self.root / "ColabDesign"
        module_dir = (
            repository / "colabdesign" / "af" / "alphafold" / "model"
        )
        module_dir.mkdir(parents=True)
        module = module_dir / "modules.py"
        module.write_text("def relative(batch): return batch['residue_index']\n")
        with self.assertRaisesRegex(ContractError, "does not consume"):
            _assert_cyclic_offset_supported(repository, use_multimer=False)
        module.write_text('def relative(batch): return "offset" in batch\n')
        _assert_cyclic_offset_supported(repository, use_multimer=False)

    def test_terminal_distance_requires_actual_c_and_n_atoms(self):
        path = self.root / "monomer.pdb"
        write_monomer(path)
        structure = parse_pdb(path)
        observed = terminal_bond_distance(structure, "B")
        first = structure.chains["B"][0].atoms["N"].coord
        last = structure.chains["B"][-1].atoms["C"].coord
        self.assertAlmostEqual(observed, float(np.linalg.norm(first - last)))

    def test_kabsch_recovers_rigid_rotation_and_translation(self):
        reference = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
            [0.0, 0.0, 3.0],
        ])
        rotation = np.array([
            [0.0, -1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
        ])
        mobile = reference @ rotation + np.array([4.0, -3.0, 2.0])
        aligned = apply_transform(mobile, kabsch_transform(mobile, reference))
        self.assertLess(rmsd(aligned, reference), 1e-10)

    def test_ipsae_matches_official_residue_specific_d0_definition(self):
        pae = np.full((5, 5), 30.0)
        labels = ["A", "A", "B", "B", "B"]
        pae[0, 2:] = [0.5, 1.0, 20.0]
        pae[2:, 0] = [0.5, 1.0, 20.0]
        result = calculate_ipsae(pae, labels, "A", "B", 10.0)
        expected_ab = np.mean([1 / (1 + 0.5 ** 2), 1 / (1 + 1.0 ** 2)])
        self.assertAlmostEqual(result["ipsae_asym"]["A->B"]["score"], expected_ab)
        self.assertAlmostEqual(result["ipsae"], 1 / (1 + 0.5 ** 2))
        self.assertEqual(result["ipsae_asym"]["A->B"]["n0res"], 2)

    def test_external_tool_parsers_fail_closed(self):
        self.assertEqual(parse_prodigy_output("-10.25")["dg"], -10.25)
        self.assertEqual(
            parse_prodigy_output("prediction_model0\t  -6.111\n")["dg"],
            -6.111,
        )
        rosetta = parse_rosetta_interface_output(
            "SCORE: dSASA_int sc_value dG_separated description\n"
            "SCORE: 550.0 0.72 -12.0 model_1\n"
        )
        self.assertEqual(rosetta["dsasa"], 550.0)
        self.assertEqual(rosetta["sc"], 0.72)
        with self.assertRaises(ContractError):
            parse_rosetta_interface_output("SCORE: dG_separated description\nSCORE: -12 x\n")


class PredictionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prediction-pipeline-test-"))
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
        self.artifacts_root = self.root / "artifacts"
        self.run_root = self.root / "runs"

    def _register_candidate(self, *, legacy_sequence=SEQUENCE):
        design_dir = self.root / "design" / "C0001"
        design_dir.mkdir(parents=True)
        legacy = design_dir / "refold.pdb"
        reference = design_dir / "backbone.pdb"
        write_monomer(legacy, legacy_sequence)
        write_monomer(reference)
        manifest = {
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "length": len(SEQUENCE),
            "source_route": "test_route",
            "source_batch": "test_batch",
            "cyclization_type": "head-to-tail_amide",
            "refold_pdb": str(legacy),
            "refold_pdb_hash": hashlib.sha256(legacy.read_bytes()).hexdigest()[:12],
            "backbone_pdb": str(reference),
            "backbone_pdb_hash": hashlib.sha256(reference.read_bytes()).hexdigest()[:12],
        }
        manifest_path = design_dir / "manifest.json"
        manifest["manifest_path"] = str(manifest_path)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        CandidateIndex.add({
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "source_route": "test_route",
            "source_batch": "test_batch",
            "cyclization_type": "head-to-tail_amide",
            "design_pdb_path": str(legacy),
            "design_pdb_hash": manifest["refold_pdb_hash"],
            "manifest_path": str(manifest_path),
        })
        return reference

    def _write_complete_artifacts(self, reference: Path):
        candidate_dir = self.artifacts_root / "C0001"
        candidate_dir.mkdir(parents=True)
        monomer = candidate_dir / "monomer.pdb"
        post = candidate_dir / "post_relax.pdb"
        write_monomer(monomer)
        write_monomer(post)
        targets = {}
        for target_id in ("MDM2", "MDMX"):
            predictions = []
            for index, (predictor, seed, shift) in enumerate((
                ("ColabDesign", 0, (0, 1.5, 0)),
                ("ColabDesign", 1, (0, 1.6, 0)),
                ("ColabFold", 2, (0, 1.55, 0)),
            )):
                pdb = candidate_dir / f"{target_id}_{index}.pdb"
                pae = candidate_dir / f"{target_id}_{index}_pae.json"
                metadata = candidate_dir / f"{target_id}_{index}_metadata.json"
                write_complex(pdb, binder_shift=shift)
                n_residues = 3 + len(SEQUENCE)
                pae.write_text(
                    json.dumps({"predicted_aligned_error": np.full(
                        (n_residues, n_residues), 0.1
                    ).tolist()}),
                    encoding="utf-8",
                )
                metadata.write_text(json.dumps({
                    "requested_sequence": SEQUENCE,
                    "observed_sequence": SEQUENCE,
                    "binder_chain": "B",
                    "iptm": 0.9,
                }), encoding="utf-8")
                predictions.append({
                    "predictor": predictor,
                    "seed": seed,
                    "primary": index == 0,
                    "pdb": pdb.name,
                    "pae": pae.name,
                    "metadata": metadata.name,
                    "binder_chain": "B",
                })
            prodigy = candidate_dir / f"{target_id}_prodigy.txt"
            rosetta = candidate_dir / f"{target_id}_rosetta.sc"
            prodigy.write_text("-10.5\n", encoding="utf-8")
            rosetta.write_text(
                "SCORE: dSASA_int sc_value dG_separated description\n"
                "SCORE: 550.0 0.75 -12.0 model\n",
                encoding="utf-8",
            )
            targets[target_id] = {
                "target_chain": "A",
                "complex_predictions": predictions,
                "prodigy_output": prodigy.name,
                "rosetta_output": rosetta.name,
            }
        bundle = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "global": {
                "monomer_predictions": [{
                    "predictor": "ColabDesign",
                    "seed": 0,
                    "primary": True,
                    "pdb": monomer.name,
                }],
                "post_relax_pdb": post.name,
                "design_reference_pdb": str(reference),
            },
            "targets": targets,
        }
        (candidate_dir / "artifacts.json").write_text(
            json.dumps(bundle), encoding="utf-8"
        )

    def _pipeline(self, *, thresholds, project=None, run_id="test_run", resume=False):
        return PredictionPipeline(
            candidate_rows=CandidateIndex.load(),
            project=project or project_config(),
            thresholds=thresholds,
            artifacts_root=self.artifacts_root,
            run_root=self.run_root,
            config=PredictionConfig(),
            run_id=run_id,
            resume=resume,
        )

    def test_missing_artifacts_and_thresholds_are_pending_without_fake_values(self):
        self._register_candidate()
        summary = self._pipeline(thresholds={}, project=project_config(("MDM2",))).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        row = CandidateIndex.find("C0001")
        metrics = json.loads(row["metrics_json"])
        self.assertEqual(metrics["global"], {})
        self.assertEqual(metrics["targets"], {})
        self.assertEqual(row["final_status"], "prediction_pending")
        self.assertNotEqual(row["plddt"], "0")

    def test_sequence_drift_in_design_refold_is_invalid(self):
        self._register_candidate(legacy_sequence="AAAAAAAA")
        summary = self._pipeline(
            thresholds=justified_thresholds(),
            project=project_config(("MDM2",)),
        ).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "structure_sequence_mismatch")

    def test_complete_evidence_finalizes_and_resume_is_cached(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"finalized": 1})
        self.assertEqual(summary["finalized"], ["C0001"])
        row = CandidateIndex.find("C0001")
        self.assertEqual(row["competition_clearance"], "True")
        self.assertEqual(row["final_status"], "finalized")
        metrics = json.loads(row["metrics_json"])
        self.assertGreater(metrics["targets"]["MDM2"]["ipsae"], 0.9)
        self.assertIn("prediction", metrics)

        resumed = self._pipeline(
            thresholds=justified_thresholds(), resume=True
        ).run()
        self.assertEqual(resumed["cache_hits"], 1)
        notes = CandidateIndex.find("C0001")["notes"]
        self.assertEqual(notes.count("prediction_run=test_run"), 1)

    def test_withdrawn_artifacts_clear_authoritative_and_display_metrics(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        first = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(first["status_counts"], {"finalized": 1})

        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle_path.unlink()
        resumed = self._pipeline(
            thresholds=justified_thresholds(), resume=True
        ).run()

        self.assertEqual(resumed["cache_hits"], 0)
        self.assertEqual(resumed["status_counts"], {"prediction_pending": 1})
        row = CandidateIndex.find("C0001")
        metrics = json.loads(row["metrics_json"])
        self.assertEqual(metrics["global"], {})
        self.assertEqual(metrics["targets"], {})
        self.assertEqual(row["plddt"], "")
        self.assertEqual(row["ipsae_mdm2"], "")
        self.assertEqual(row["dg_mdmx"], "")
        self.assertEqual(row["competition_clearance"], "False")
        self.assertEqual(row["final_status"], "prediction_pending")
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["status"], "prediction_pending")
        self.assertEqual(record["metrics"], {"global": {}, "targets": {}})

    def test_invalidated_artifact_withdraws_previously_finalized_metrics(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        pdb_path = self.artifacts_root / "C0001" / "MDM2_0.pdb"
        bundle["targets"]["MDM2"]["complex_predictions"][0]["pdb_sha256"] = (
            hashlib.sha256(pdb_path.read_bytes()).hexdigest()
        )
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        first = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(first["status_counts"], {"finalized": 1})

        pdb_path.write_text(pdb_path.read_text() + "REMARK tampered\n")
        resumed = self._pipeline(
            thresholds=justified_thresholds(), resume=True
        ).run()

        self.assertEqual(resumed["status_counts"], {"invalid": 1})
        row = CandidateIndex.find("C0001")
        metrics = json.loads(row["metrics_json"])
        self.assertEqual(metrics["global"], {})
        self.assertEqual(metrics["targets"], {})
        self.assertEqual(metrics["prediction"]["evidence_status"], "invalid")
        self.assertEqual(row["plddt"], "")
        self.assertEqual(row["ipsae_mdm2"], "")
        self.assertEqual(row["all_layers_pass"], "False")
        self.assertEqual(row["competition_clearance"], "False")
        self.assertEqual(row["triage_status"], "invalid")
        self.assertEqual(row["final_status"], "invalid")

    def test_unjustified_thresholds_cannot_finalize(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        thresholds = justified_thresholds()
        for value in thresholds.values():
            value.pop("source", None)
            value.pop("evidence_grade", None)
        summary = self._pipeline(thresholds=thresholds).run()
        self.assertEqual(
            summary["status_counts"], {"awaiting_threshold_calibration": 1}
        )
        row = CandidateIndex.find("C0001")
        self.assertEqual(row["metric_clearance"], "True")
        self.assertEqual(row["competition_clearance"], "False")

    def test_partial_real_bundle_stays_recoverably_pending(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["global"].pop("post_relax_pdb")
        for target in bundle["targets"].values():
            target.pop("rosetta_output")
            for prediction in target["complex_predictions"]:
                prediction["predictor"] = "ColabDesign"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        codes = {issue["code"] for issue in record["issues"]}
        self.assertIn("l3_physics_missing", codes)
        self.assertIn("l4_post_relax_missing", codes)
        self.assertIn("l6_predictors_insufficient", codes)
        self.assertEqual(record["metrics"]["targets"]["MDM2"]["dg"], -10.5)

    def test_declared_artifact_hash_mismatch_is_invalid(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["global"]["monomer_predictions"][0]["pdb_sha256"] = "0" * 64
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "artifact_hash_mismatch")

    def test_invalid_iptm_metadata_is_rejected_instead_of_propagating_nan(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        metadata_path = self.artifacts_root / "C0001" / "MDM2_0_metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["iptm"] = "NaN"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(
            record["issues"][0]["code"], "prediction_metadata_value_invalid"
        )

    def test_changed_approved_project_is_rejected_before_run(self):
        self._register_candidate()
        project = project_config(("MDM2",))
        project["targets"][0]["binding_site"]["residues"].append(99)
        with self.assertRaisesRegex(ContractError, "approved_digest"):
            self._pipeline(
                thresholds=justified_thresholds(), project=project
            )


if __name__ == "__main__":
    unittest.main()
