"""Research/Design v5 可靠性回归测试；所有运行时文件写入临时目录。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-reliability-test-"))
os.environ["CYCPEP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["CYCPEP_EVIDENCE_DIR"] = str(TEST_ROOT / "evidence")
os.environ["CYCPEP_DESIGN_ROOT"] = str(TEST_ROOT / "designs")

from agents.design import design_atsp_cyclize, design_motif_graft
from agents.research import _build_dynamic_pockets
from data_layer import CandidateIndex
from scripts import search_pdb
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
    def test_route_b_does_not_register_templates_when_adapter_is_missing(self):
        with patch("agents.design._proteinmpnn_adapter_available", return_value=False):
            candidates = design_motif_graft(10)
        self.assertEqual(candidates, [])
        self.assertEqual(CandidateIndex.load(), [])

    def test_route_c_generates_200_unique_head_to_tail_manifests(self):
        candidates = design_atsp_cyclize(200)
        self.assertEqual(len(candidates), 200)
        self.assertEqual(len({candidate["sequence"] for candidate in candidates}), 200)
        self.assertEqual(len(CandidateIndex.load()), 200)
        for candidate in candidates:
            self.assertEqual(candidate["cyclization_type"], "head_to_tail_amide")
            self.assertTrue(Path(candidate["manifest_path"]).exists())


if __name__ == "__main__":
    unittest.main()
