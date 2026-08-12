"""Prediction pipeline integration tests; no GPU or external tools required."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import data_layer
from data_layer import CandidateIndex, State
from prediction_pipeline.contracts import (
    ContractError,
    PredictionConfig,
)
from prediction_pipeline.pipeline import PredictionPipeline
from prediction_pipeline.relax_worker import (
    POST_RELAX_PROTOCOL,
    POST_RELAX_TOOL,
    PYROSETTA_VERSION,
)
from prediction_pipeline.structures import (
    parse_pdb,
    terminal_bond_distance,
)

from _prediction_test_utils import (
    SEQUENCE,
    chain_pdb,
    justified_thresholds,
    project_config,
    write_complex,
    write_monomer,
)

class PredictionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prediction-pipeline-test-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
        self.artifacts_root = self.root / "artifacts"
        self.run_root = self.root / "runs"

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    def _register_candidate(self, *, legacy_sequence=SEQUENCE):
        design_dir = self.root / "design" / "C0001"
        design_dir.mkdir(parents=True)
        legacy = design_dir / "refold.pdb"
        reference = design_dir / "backbone.pdb"
        write_monomer(legacy, legacy_sequence)
        # Keep coordinates aligned while making this a distinct immutable file.
        write_monomer(reference, bfactor=80.0)
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
        post_metadata = candidate_dir / "post_relax_metadata.json"
        write_monomer(monomer)
        write_monomer(post)
        pre_structure = parse_pdb(monomer)
        post_structure = parse_pdb(post)
        pre_distance = terminal_bond_distance(pre_structure, "B")
        post_distance = terminal_bond_distance(post_structure, "B")
        post_metadata.write_text(json.dumps({
            "tool": POST_RELAX_TOOL,
            "tool_version": PYROSETTA_VERSION,
            "protocol": POST_RELAX_PROTOCOL,
            "input_pdb_sha256": hashlib.sha256(monomer.read_bytes()).hexdigest(),
            "output_pdb_sha256": hashlib.sha256(post.read_bytes()).hexdigest(),
            "sequence": SEQUENCE,
            "input_chain": "B",
            "output_chain": "B",
            "cyclization_type": "head_to_tail_amide",
            "bond_topology_applied": True,
            "topology_geometry_constraints_applied": True,
            "terminal_c_to_n_distance_pre_angstrom": pre_distance,
            "terminal_c_to_n_distance_post_angstrom": post_distance,
            "backbone_rmsd_to_input_angstrom": 0.0,
            "pre_total_score_ref2015": 10.0,
            "post_total_score_ref2015": 8.0,
            "seed": 101,
            "repeats": 3,
            "coordinate_constraints": {
                "enabled": True,
                "to_start_coordinates": True,
                "sidechains": False,
                "ramp_down": False,
                "stdev_angstrom": 0.5,
            },
            "design_enabled": False,
        }), encoding="utf-8")
        targets = {}
        for target_id in ("MDM2", "MDMX"):
            predictions = []
            for index, (predictor, seed, shift) in enumerate((
                ("ColabDesign", 0, (0, 1.5, 0)),
                ("ColabDesign", 1, (0, 1.6, 0)),
                ("Boltz", 2, (0, 1.55, 0)),
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
                    "tool": predictor,
                    "tool_commit": "a" * 40 if predictor == "ColabDesign" else None,
                    "tool_version": "1.0.0" if predictor == "Boltz" else None,
                    "model_family": (
                        "AlphaFold2" if predictor == "ColabDesign" else "Boltz-1"
                    ),
                    "model_id": (
                        f"alphafold2_model_{index}"
                        if predictor == "ColabDesign" else "boltz1_model_0"
                    ),
                    "seed": seed,
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
        from prediction_pipeline.protocol import protocol_binding
        bundle = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "protocol": protocol_binding(),
            "global": {
                "monomer_predictions": [{
                    "predictor": "ColabDesign",
                    "seed": 0,
                    "primary": True,
                    "pdb": monomer.name,
                }],
                "post_relax_pdb": post.name,
                "post_relax_metadata": post_metadata.name,
                "design_reference_pdb": str(reference),
            },
            "targets": targets,
        }
        (candidate_dir / "artifacts.json").write_text(
            json.dumps(bundle), encoding="utf-8"
        )

    def _pipeline(
        self, *, thresholds, project=None, run_id="test_run", resume=False,
        require_protocol_compatibility=True, calibration_binding=None,
    ):
        return PredictionPipeline(
            candidate_rows=CandidateIndex.load(),
            project=project or project_config(),
            thresholds=thresholds,
            calibration_binding=calibration_binding,
            artifacts_root=self.artifacts_root,
            run_root=self.run_root,
            config=PredictionConfig(),
            run_id=run_id,
            resume=resume,
            require_protocol_compatibility=require_protocol_compatibility,
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

    def test_stale_protocol_bundle_rejected_before_scoring(self):
        # P1: a bundle bound to an older protocol must not be scored and
        # written into formal records, even via agents/prediction.py --resume.
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        data["protocol"] = {"name": "old", "version": "old", "sha256": "b" * 64}
        bundle_path.write_text(json.dumps(data), encoding="utf-8")
        summary = self._pipeline(
            thresholds={}, project=project_config(("MDM2", "MDMX"))
        ).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["status"], "invalid")
        self.assertEqual(record["metrics"], {"global": {}, "targets": {}})
        self.assertEqual(record["issues"][0]["code"], "bundle_protocol_mismatch")

    def test_replay_mode_reads_stale_bundle(self):
        # Replay/audit tools keep historical evidence readable.
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        data["protocol"] = {"name": "old", "version": "old", "sha256": "b" * 64}
        bundle_path.write_text(json.dumps(data), encoding="utf-8")
        summary = self._pipeline(
            thresholds={},
            project=project_config(("MDM2", "MDMX")),
            require_protocol_compatibility=False,
        ).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})

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

    def test_pipeline_restores_reviewed_hotspot_numbers_after_predictor_renumbering(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        reviewed_target = self.root / "reviewed_target.pdb"
        content, _ = chain_pdb(
            "AAA", "A", residue_numbers=[53, 92, 95]
        )
        reviewed_target.write_text(content + "END\n", encoding="utf-8")

        project = project_config()
        for target in project["targets"]:
            target["structure"].update({
                "coordinate_path": str(reviewed_target),
                "coordinate_sha256": hashlib.sha256(
                    reviewed_target.read_bytes()
                ).hexdigest(),
            })
            target["binding_site"]["residues"] = [53, 92, 95]
        content_for_digest = json.loads(json.dumps(project))
        content_for_digest.pop("review")
        project["review"]["approved_digest"] = hashlib.sha256(
            json.dumps(
                content_for_digest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        summary = self._pipeline(
            thresholds=justified_thresholds(), project=project
        ).run()
        self.assertEqual(summary["status_counts"], {"finalized": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        for target_id in ("MDM2", "MDMX"):
            self.assertEqual(
                record["metrics"]["targets"][target_id]["hotspot_cov"], 1.0
            )
        hotspot_provenance = [
            item for item in record["provenance"]
            if item.get("metric") == "targets.MDMX.hotspot_cov"
        ][0]
        self.assertEqual(
            hotspot_provenance["aggregation"],
            "median_hotspot_and_strict_majority_site",
        )
        self.assertEqual(len(hotspot_provenance["samples"]), 3)
        for sample in hotspot_provenance["samples"]:
            self.assertEqual(sample["details"]["covered_hotspots"], [53, 92, 95])
            self.assertEqual(
                sample["target_numbering"]["mapping"],
                "reviewed_target_sequence_order",
            )

    def test_l5_aggregates_all_models_when_primary_is_an_outlier(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        for target_id in ("MDM2", "MDMX"):
            write_complex(
                self.artifacts_root / "C0001" / f"{target_id}_0.pdb",
                binder_shift=(100.0, 100.0, 100.0),
            )

        self._pipeline(thresholds=justified_thresholds()).run()
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        for target_id in ("MDM2", "MDMX"):
            target = record["metrics"]["targets"][target_id]
            self.assertEqual(target["hotspot_cov"], 1.0)
            self.assertTrue(target["site_consistency"])
            self.assertAlmostEqual(target["site_consistency_fraction"], 2 / 3)
            provenance = next(
                item for item in record["provenance"]
                if item.get("metric") == f"targets.{target_id}.hotspot_cov"
            )
            self.assertEqual(
                [sample["details"]["hotspot_cov"] for sample in provenance["samples"]],
                [0.0, 1.0, 1.0],
            )

    def test_prodigy_ensemble_uses_median_and_links_each_prediction(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target_id, target in bundle["targets"].items():
            target.pop("prodigy_output")
            outputs = []
            for index, value in enumerate((-2.0, -10.0, -8.0)):
                output = self.artifacts_root / "C0001" / f"{target_id}_{index}_prodigy.txt"
                output.write_text(f"{value}\n", encoding="utf-8")
                prediction = target["complex_predictions"][index]
                metadata = json.loads(
                    (self.artifacts_root / "C0001" / prediction["metadata"]).read_text()
                )
                prediction_pdb = self.artifacts_root / "C0001" / prediction["pdb"]
                outputs.append({
                    "predictor": prediction["predictor"],
                    "model_id": metadata["model_id"],
                    "seed": prediction["seed"],
                    "prediction_pdb_sha256": hashlib.sha256(
                        prediction_pdb.read_bytes()
                    ).hexdigest(),
                    "output": output.name,
                })
            target["prodigy_outputs"] = outputs
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        self._pipeline(thresholds=justified_thresholds()).run()
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        for target_id in ("MDM2", "MDMX"):
            self.assertEqual(record["metrics"]["targets"][target_id]["dg"], -8.0)
            provenance = next(
                item for item in record["provenance"]
                if item.get("tool") == "PRODIGY"
                and item.get("metric_target") == target_id
            )
            self.assertEqual(
                provenance["aggregation"], "median_across_declared_predictions"
            )
            self.assertEqual(len(provenance["samples"]), 3)

    def test_prodigy_ensemble_rejects_incomplete_prediction_coverage(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        target = bundle["targets"]["MDM2"]
        target.pop("prodigy_output")
        prediction = target["complex_predictions"][0]
        metadata = json.loads(
            (self.artifacts_root / "C0001" / prediction["metadata"]).read_text()
        )
        prediction_pdb = self.artifacts_root / "C0001" / prediction["pdb"]
        output = self.artifacts_root / "C0001" / "MDM2_partial_prodigy.txt"
        output.write_text("-8.0\n", encoding="utf-8")
        target["prodigy_outputs"] = [{
            "predictor": prediction["predictor"],
            "model_id": metadata["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": hashlib.sha256(
                prediction_pdb.read_bytes()
            ).hexdigest(),
            "output": output.name,
        }]
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "prodigy_coverage_mismatch")

    def test_rosetta_ensemble_uses_median_and_links_each_prediction(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target_id, target in bundle["targets"].items():
            target.pop("rosetta_output")
            outputs = []
            values = ((200.0, 0.2, -2.0), (600.0, 0.8, -12.0), (400.0, 0.6, -8.0))
            for index, (dsasa, sc, dg) in enumerate(values):
                output = self.artifacts_root / "C0001" / f"{target_id}_{index}_rosetta.sc"
                output.write_text(
                    "SCORE: dSASA_int sc_value dG_separated description\n"
                    f"SCORE: {dsasa} {sc} {dg} model_{index}\n",
                    encoding="utf-8",
                )
                prediction = target["complex_predictions"][index]
                prediction_metadata = json.loads(
                    (self.artifacts_root / "C0001" / prediction["metadata"]).read_text()
                )
                prediction_pdb = self.artifacts_root / "C0001" / prediction["pdb"]
                score_metadata = (
                    self.artifacts_root / "C0001"
                    / f"{target_id}_{index}_rosetta_metadata.json"
                )
                score_metadata.write_text(json.dumps({
                    "tool": "PyRosetta InterfaceAnalyzerMover",
                    "tool_version_output": "test",
                    "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
                    "predictor": prediction["predictor"],
                    "model_id": prediction_metadata["model_id"],
                    "seed": prediction["seed"],
                    "prediction_pdb_sha256": hashlib.sha256(
                        prediction_pdb.read_bytes()
                    ).hexdigest(),
                    "target_chain": "A",
                    "binder_chain": "B",
                    "binder_sequence": SEQUENCE,
                    "terminal_c_to_n_distance_angstrom": 1.3,
                    "declared_bond": {"res1": 10, "atom1": "C", "res2": 3, "atom2": "N"},
                    "scorefunction": "ref2015",
                    "metrics": {
                        "dsasa": dsasa,
                        "sc": sc,
                        "rosetta_dg_separated": dg,
                    },
                    "xml_sha256": "a" * 64,
                }), encoding="utf-8")
                outputs.append({
                    "predictor": prediction["predictor"],
                    "model_id": prediction_metadata["model_id"],
                    "seed": prediction["seed"],
                    "prediction_pdb_sha256": hashlib.sha256(
                        prediction_pdb.read_bytes()
                    ).hexdigest(),
                    "output": output.name,
                    "metadata": score_metadata.name,
                })
            target["rosetta_outputs"] = outputs
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        self._pipeline(thresholds=justified_thresholds()).run()
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        for target_id in ("MDM2", "MDMX"):
            target_metrics = record["metrics"]["targets"][target_id]
            self.assertEqual(target_metrics["dsasa"], 400.0)
            self.assertEqual(target_metrics["sc"], 0.6)
            self.assertEqual(target_metrics["rosetta_dg_separated"], -8.0)
            provenance = next(
                item for item in record["provenance"]
                if item.get("tool") == "Rosetta InterfaceAnalyzer"
                and item.get("metric_target") == target_id
            )
            self.assertEqual(
                provenance["aggregation"], "median_across_declared_predictions"
            )
            self.assertEqual(len(provenance["samples"]), 3)

    def test_rosetta_ensemble_rejects_incomplete_prediction_coverage(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        target = bundle["targets"]["MDM2"]
        target.pop("rosetta_output")
        prediction = target["complex_predictions"][0]
        metadata = json.loads(
            (self.artifacts_root / "C0001" / prediction["metadata"]).read_text()
        )
        prediction_pdb = self.artifacts_root / "C0001" / prediction["pdb"]
        output = self.artifacts_root / "C0001" / "MDM2_partial_rosetta.sc"
        output.write_text(
            "SCORE: dSASA_int sc_value description\nSCORE: 400 0.6 model\n",
            encoding="utf-8",
        )
        score_metadata = self.artifacts_root / "C0001" / "MDM2_partial_rosetta.json"
        score_metadata.write_text(json.dumps({
            "tool": "PyRosetta InterfaceAnalyzerMover",
            "tool_version_output": "test",
            "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
            "predictor": prediction["predictor"],
            "model_id": metadata["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": hashlib.sha256(
                prediction_pdb.read_bytes()
            ).hexdigest(),
            "target_chain": "A",
            "binder_chain": "B",
            "binder_sequence": SEQUENCE,
            "terminal_c_to_n_distance_angstrom": 1.3,
            "declared_bond": {"res1": 10, "atom1": "C", "res2": 3, "atom2": "N"},
            "scorefunction": "ref2015",
            "metrics": {"dsasa": 400.0, "sc": 0.6},
            "xml_sha256": "a" * 64,
        }), encoding="utf-8")
        target["rosetta_outputs"] = [{
            "predictor": prediction["predictor"],
            "model_id": metadata["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": hashlib.sha256(
                prediction_pdb.read_bytes()
            ).hexdigest(),
            "output": output.name,
            "metadata": score_metadata.name,
        }]
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "rosetta_coverage_mismatch")

    def test_rosetta_ensemble_rejects_missing_topology_metadata(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        target = bundle["targets"]["MDM2"]
        target.pop("rosetta_output")
        prediction = target["complex_predictions"][0]
        prediction_metadata = json.loads(
            (self.artifacts_root / "C0001" / prediction["metadata"]).read_text()
        )
        prediction_pdb = self.artifacts_root / "C0001" / prediction["pdb"]
        output = self.artifacts_root / "C0001" / "MDM2_rosetta_without_metadata.sc"
        output.write_text(
            "SCORE: dSASA_int sc_value description\nSCORE: 400 0.6 model\n",
            encoding="utf-8",
        )
        target["rosetta_outputs"] = [{
            "predictor": prediction["predictor"],
            "model_id": prediction_metadata["model_id"],
            "seed": prediction["seed"],
            "prediction_pdb_sha256": hashlib.sha256(
                prediction_pdb.read_bytes()
            ).hexdigest(),
            "output": output.name,
        }]
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "rosetta_metadata_missing")

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
        bundle["global"].pop("post_relax_metadata")
        for target in bundle["targets"].values():
            target.pop("rosetta_output")
            for prediction in target["complex_predictions"]:
                prediction["predictor"] = "ColabDesign"
                metadata_path = bundle_path.parent / prediction["metadata"]
                metadata = json.loads(metadata_path.read_text())
                metadata.update({
                    "tool": "ColabDesign",
                    "tool_commit": "a" * 40,
                    "model_family": "AlphaFold2",
                    "model_id": f"alphafold2_model_{prediction['seed']}",
                    "seed": prediction["seed"],
                })
                metadata.pop("tool_version", None)
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
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

    def test_l4_requires_relax_provenance_before_using_post_structure(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["global"].pop("post_relax_metadata")
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        codes = {issue["code"] for issue in record["issues"]}
        self.assertIn("l4_post_relax_provenance_missing", codes)
        self.assertNotIn("nc_distance_post", record["metrics"]["global"])

    def test_l4_rejects_unpinned_relax_protocol(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        metadata_path = bundle_path.parent / "post_relax_metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["protocol"] = "unreviewed_relax"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "post_relax_protocol_mismatch")

    def test_l4_rejects_relax_geometry_metadata_drift(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        metadata_path = bundle_path.parent / "post_relax_metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["backbone_rmsd_to_input_angstrom"] = 1.0
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(record["issues"][0]["code"], "post_relax_geometry_mismatch")

    def test_l6_rejects_relabelled_predictor(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target in bundle["targets"].values():
            target["complex_predictions"][2]["predictor"] = "IndependentModel"
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"invalid": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        self.assertEqual(
            record["issues"][0]["code"], "prediction_predictor_mismatch"
        )

    def test_l6_duplicate_predictor_seed_stays_pending(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target in bundle["targets"].values():
            duplicate = target["complex_predictions"][1]
            duplicate["seed"] = 0
            metadata_path = bundle_path.parent / duplicate["metadata"]
            metadata = json.loads(metadata_path.read_text())
            metadata["seed"] = 0
            metadata["model_id"] = "alphafold2_model_0"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        codes = {issue["code"] for issue in record["issues"]}
        self.assertIn("l6_prediction_duplicate", codes)

    def test_l6_requires_independent_model_families(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target in bundle["targets"].values():
            third = target["complex_predictions"][2]
            third["predictor"] = "ColabFold"
            metadata_path = bundle_path.parent / third["metadata"]
            metadata = json.loads(metadata_path.read_text())
            metadata.update({
                "tool": "ColabFold",
                "tool_version": "1.5.5",
                "model_family": "AlphaFold2",
                "model_id": "alphafold2_model_2",
                "seed": third["seed"],
            })
            metadata.pop("tool_commit", None)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        codes = {issue["code"] for issue in record["issues"]}
        self.assertIn("l6_predictors_insufficient", codes)

    def test_l6_rejects_duplicate_pdb_content(self):
        reference = self._register_candidate()
        self._write_complete_artifacts(reference)
        bundle_path = self.artifacts_root / "C0001" / "artifacts.json"
        bundle = json.loads(bundle_path.read_text())
        for target in bundle["targets"].values():
            target["complex_predictions"][2]["pdb"] = (
                target["complex_predictions"][0]["pdb"]
            )
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

        summary = self._pipeline(thresholds=justified_thresholds()).run()
        self.assertEqual(summary["status_counts"], {"prediction_pending": 1})
        record = json.loads(
            (self.run_root / "test_run" / "records" / "C0001.json").read_text()
        )
        codes = {issue["code"] for issue in record["issues"]}
        self.assertIn("l6_prediction_duplicate", codes)

    def test_prediction_preserves_design_candidate_counter(self):
        self._register_candidate()
        state = State.load()
        state["candidate_count"] = 1270
        State.save(state)

        summary = self._pipeline(
            thresholds={}, project=project_config(("MDM2",))
        ).run()

        self.assertEqual(State.load()["candidate_count"], 1270)
        self.assertEqual(summary["pipeline_version"], "1.5.1")
        self.assertEqual(State.load()["prediction"]["pipeline_version"], "1.5.1")

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
