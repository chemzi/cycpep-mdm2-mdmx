"""Research/Design v5 可靠性回归测试；所有运行时文件写入临时目录。"""

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-reliability-test-"))
os.environ["CYCPEP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["CYCPEP_EVIDENCE_DIR"] = str(TEST_ROOT / "evidence")
os.environ["CYCPEP_DESIGN_ROOT"] = str(TEST_ROOT / "designs")

import agents.design as design_module
import data_layer
from agents.design import design_atsp_cyclize, design_motif_graft
from agents.research import _build_dynamic_pockets
from data_layer import CandidateIndex, State
from project_config import load_project_config
from target_bootstrap import config_digest
from scripts import search_pdb
from scripts.compute_interface import _limit_complexes_by_target
from scripts.enrich_pdb import enrich
from scripts.aggregate_pockets import aggregate
from scripts.superpose_analyze import _global_align_pairs


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._payload


class ResearchReliabilityTests(unittest.TestCase):
    def test_interface_selection_uses_configured_target_names(self):
        targets, selected = _limit_complexes_by_target([
            {"pdb_id": "7K2E", "target": "KEAP1"},
            {"pdb_id": "7K2F", "target": "KEAP1"},
            {"pdb_id": "1YCR", "target": "MDM2"},
        ], 1)
        self.assertEqual(targets, ["KEAP1", "MDM2"])
        self.assertEqual(
            [(row["target"], row["pdb_id"]) for row in selected],
            [("KEAP1", "7K2E"), ("MDM2", "1YCR")],
        )

    def test_search_query_uses_uniprot_accession(self):
        captured = {}

        def fake_execute(query, label):
            captured["query"] = query
            captured["label"] = label
            return []

        with patch.object(search_pdb, "_execute", fake_execute):
            search_pdb._search_target("MDM2", "Q00987")
        encoded = json.dumps(captured["query"])
        self.assertIn("Q00987", encoded)
        self.assertIn("database_accession", encoded)
        self.assertNotIn('"service": "full_text"', encoded)

    def test_enrich_classifies_target_from_uniprot_and_preserves_chain_ids(self):
        graph = {
            "data": {
                "entry": {
                    "rcsb_id": "TEST",
                    "exptl": [{"method": "X-RAY DIFFRACTION"}],
                    "polymer_entities": [
                        {
                            "rcsb_id": "TEST_1",
                            "rcsb_polymer_entity_container_identifiers": {
                                "uniprot_ids": ["O15151"],
                                "asym_ids": ["A"],
                                "auth_asym_ids": ["X"],
                            },
                            "entity_poly": {
                                "pdbx_seq_one_letter_code_can": "M" * 100,
                                "type": "polypeptide(L)",
                            },
                        },
                        {
                            "rcsb_id": "TEST_2",
                            "rcsb_polymer_entity_container_identifiers": {
                                "uniprot_ids": [],
                                "asym_ids": ["B"],
                                "auth_asym_ids": ["P"],
                            },
                            "entity_poly": {
                                "pdbx_seq_one_letter_code_can": "TSFAEYWNLLSP",
                                "type": "polypeptide(L)",
                            },
                        },
                    ],
                }
            }
        }
        with patch("scripts.enrich_pdb.urllib.request.urlopen", return_value=_Response(graph)):
            result = enrich("TEST")
        self.assertEqual(result["targets_present"], ["MDMX"])
        self.assertEqual(result["target_chains"][0]["chain_ids"], ["X"])
        self.assertEqual(result["peptide_chains"][0]["chain_ids"], ["P"])

    def test_dynamic_dual_pockets_require_both_targets(self):
        aggregate = {
            "n_mdm2_structures": 10,
            "n_mdmx_structures": 0,
            "MDM2": {},
            "MDMX": {},
        }
        self.assertIsNone(_build_dynamic_pockets(aggregate, {}))

    def test_sequence_alignment_handles_numbering_offset(self):
        pairs = _global_align_pairs("ABCDEFG", "XABCDEFG")
        self.assertEqual(pairs[0], (0, 1))
        self.assertEqual(pairs[-1], (6, 7))

    def test_pocket_aggregation_ignores_chain_label_but_matches_residue_identity(self):
        entries = [
            {
                "target": "MDM2",
                "interface_target_residues": ["A:58GLY", "A:61ILE"],
            },
            {
                "target": "MDM2",
                "interface_target_residues": ["X:58GLY", "X:61ILE"],
            },
        ]
        result = aggregate("MDM2", entries)
        self.assertEqual(result["consensus_residues"]["58GLY"]["frequency"], 1.0)
        self.assertIn("58GLY", result["pocket_consensus"]["Phe19_pocket"])


class DesignReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target_pdb = TEST_ROOT / "approved_target.pdb"
        cls.target_pdb.write_text(
            "ATOM      1  CA  ALA A  25       1.000   2.000   3.000  1.00  0.00           C  \n"
            # Include binding-site residues for both MDM2 ([54,93,96]) and
            # MDMX ([53,92,95]) so _pdb_residue_range's P0 hotspot-validation
            # gate does not block tests that mock downstream steps.
            "ATOM      2  CA  ALA A  53       2.000   3.000   4.000  1.00  0.00           C  \n"
            "ATOM      3  CA  ALA A  54       2.000   3.000   4.000  1.00  0.00           C  \n"
            "ATOM      4  CA  ALA A  92       3.000   4.000   5.000  1.00  0.00           C  \n"
            "ATOM      5  CA  ALA A  93       3.000   4.000   5.000  1.00  0.00           C  \n"
            "ATOM      6  CA  ALA A  95       4.000   5.000   6.000  1.00  0.00           C  \n"
            "ATOM      7  CA  ALA A  96       4.000   5.000   6.000  1.00  0.00           C  \n"
            "ATOM      8  CA  ALA A 109       5.000   6.000   7.000  1.00  0.00           C  \n",
            encoding="utf-8",
        )
        coordinate_sha256 = hashlib.sha256(cls.target_pdb.read_bytes()).hexdigest()
        cls.project_config = load_project_config(raw={
            "project_id": "design_v5_reliability",
            "targets": [
                {
                    "id": "MDM2",
                    "structure": {
                        "pdb_id": "1YCR", "chain": "A",
                        "coordinate_path": str(cls.target_pdb),
                        "coordinate_sha256": coordinate_sha256,
                    },
                    "binding_site": {"residues": [54, 93, 96]},
                    # Cover every Route C length produced from the 12-aa ATSP
                    # template plus the approved linker matrix (0/2/3/4/5 aa).
                    "design": {"lengths": [12, 14, 15, 16, 17]},
                },
                {
                    "id": "MDMX",
                    "structure": {
                        "pdb_id": "3DAB", "chain": "A",
                        "coordinate_path": str(cls.target_pdb),
                        "coordinate_sha256": coordinate_sha256,
                    },
                    "binding_site": {"residues": [53, 92, 95]},
                    "design": {"lengths": [12, 14, 15, 16, 17]},
                },
            ],
        })
        cls.project_config["review"] = {
            "status": "approved",
            "approved_digest": config_digest(cls.project_config),
        }

    def setUp(self):
        for path in (data_layer.STATE_PATH, data_layer.LOG_PATH, data_layer.INDEX_PATH):
            path.unlink(missing_ok=True)

    def test_route_b_does_not_register_when_backbone_generation_fails(self):
        state = State.load()
        state["known_dual_binders"] = [{"name": "PMI", "sequence": "TSFAEYWNLLSP"}]
        State.save(state)
        with (
            patch("agents.design.config.ACTIVE_PROJECT_CONFIG", self.project_config),
            patch("agents.design.route_b._run_rfdiff", return_value=False),
        ):
            candidates = design_motif_graft(10)
        self.assertEqual(candidates, [])
        self.assertEqual(CandidateIndex.load(), [])

    def test_failed_refold_cannot_reuse_stale_artifacts(self):
        candidate_dir = TEST_ROOT / "stale-refold"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        output_pdb = candidate_dir / "refold.pdb"
        score_path = Path(f"{output_pdb}.plddt")
        output_pdb.write_text("stale PDB", encoding="utf-8")
        score_path.write_text("0.99", encoding="utf-8")

        with patch(
            "agents.design.runtime.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stderr="expected failure"),
        ):
            result = design_module._run_refold("ACDEFGHI", str(output_pdb))

        self.assertIsNone(result)
        self.assertFalse(output_pdb.exists())
        self.assertFalse(score_path.exists())

    def test_refold_rejects_saved_pdb_sequence_drift(self):
        candidate_dir = TEST_ROOT / "drift-refold"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        output_pdb = candidate_dir / "refold.pdb"
        score_path = Path(f"{output_pdb}.plddt")

        def fake_success(*_args, **_kwargs):
            output_pdb.write_text(
                "ATOM      1  CA  ALA A   1       1.000   2.000   3.000"
                "  1.00 90.00           C  \n",
                encoding="utf-8",
            )
            score_path.write_text("0.90", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="")

        with patch("agents.design.runtime.subprocess.run", side_effect=fake_success):
            result = design_module._run_refold("ACDEFGHI", str(output_pdb))

        self.assertIsNone(result)

    def test_route_c_generates_200_unique_manifest_handoffs(self):
        state = State.load()
        state["known_dual_binders"] = [
            {"name": "ATSP-7041", "sequence": "LTFLEYWAAQSL"}
        ]
        State.save(state)

        def fake_refold(_sequence, output_pdb):
            Path(output_pdb).write_text(
                "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C  \n",
                encoding="utf-8",
            )
            return 0.9

        def fake_design_references(_config, batch_dir, sequences):
            root = Path(batch_dir) / "fake_route_c_references"
            root.mkdir(parents=True, exist_ok=True)
            references = {}
            for index, (_sequence, _description) in enumerate(sequences):
                path = root / f"bb_{index}.pdb"
                path.write_text(
                    f"REMARK independent Route C reference {index}\n",
                    encoding="utf-8",
                )
                references[index] = str(path)
            return references

        with (
            patch("agents.design.config.ACTIVE_PROJECT_CONFIG", self.project_config),
            patch(
                "agents.design.route_c._route_c_design_references",
                side_effect=fake_design_references,
            ),
            patch("agents.design.candidates._run_refold", side_effect=fake_refold),
            patch("agents.design.candidates._ring_closure_check", return_value={"pass": True}),
        ):
            candidates = design_atsp_cyclize(200, seed=42)
        self.assertEqual(len(candidates), 200)
        self.assertEqual(len({candidate["sequence"] for candidate in candidates}), 200)
        self.assertEqual(len(CandidateIndex.load()), 200)
        for candidate in candidates:
            self.assertTrue(candidate["cyclization_type"])
            self.assertTrue(candidate["design_pdb_path"].endswith("refold.pdb"))
            self.assertTrue(Path(candidate["manifest_path"]).exists())
            manifest = json.loads(Path(candidate["manifest_path"]).read_text())
            self.assertEqual(
                manifest["design_reference_role"],
                "rfdiffusion_target_bound_backbone",
            )
            self.assertTrue(Path(manifest["design_reference_pdb"]).is_file())
            self.assertNotEqual(
                manifest["design_reference_pdb"], manifest["refold_pdb"]
            )


if __name__ == "__main__":
    unittest.main()
