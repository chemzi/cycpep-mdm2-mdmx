"""Prediction structure / parser unit tests; no GPU or external tools required."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from prediction_pipeline.contracts import (
    ContractError,
    _validate_candidate_row,
    candidate_from_row,
)
from prediction_pipeline.colabdesign_worker import (
    _assert_cyclic_offset_supported,
    _cyclic_offset,
)
from prediction_pipeline.boltz_worker import boltz_input_yaml
from prediction_pipeline.metrics import (
    calculate_ipsae,
    parse_prodigy_output,
    parse_rosetta_interface_output,
)
from prediction_pipeline.rosetta_worker import interface_xml
from prediction_pipeline.pyrosetta_cli import _write_scorefile
from prediction_pipeline.pyrosetta_relax_cli import _normalize_pdb_embedded_path
from prediction_pipeline.relax_worker import (
    POST_RELAX_PROTOCOL,
    POST_RELAX_TOOL,
    PYROSETTA_VERSION,
    topology_xml,
)
from scripts.run_prediction_predictors import (
    parse_ensemble_members,
    require_design_references,
)
from prediction_pipeline.structures import (
    apply_transform,
    canonical_target_residue_numbers,
    exact_sequence_chain,
    interface_hotspot_metrics,
    kabsch_transform,
    mean_plddt,
    parse_pdb,
    rmsd,
    target_aligned_binder_rmsd,
    terminal_bond_distance,
)

from _prediction_test_utils import SEQUENCE, chain_pdb, write_monomer

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

    def test_seven_residue_cyclic_sequence_contract(self):
        self.assertEqual(
            _validate_candidate_row({
                "candidate_id": "C0007", "sequence": "GDEETGE"
            }),
            ("C0007", "GDEETGE"),
        )
        with self.assertRaisesRegex(ContractError, "outside 7-20"):
            _validate_candidate_row({
                "candidate_id": "C0006", "sequence": "AAAAAA"
            })

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

    def test_prediction_runner_pairs_seeds_with_distinct_af2_models(self):
        self.assertEqual(
            parse_ensemble_members("0,1,2"),
            [(0, 0), (1, 1), (2, 2)],
        )
        self.assertEqual(
            parse_ensemble_members("3,4", "1,4"),
            [(3, 1), (4, 4)],
        )
        with self.assertRaisesRegex(ContractError, "exactly one value"):
            parse_ensemble_members("0,1,2", "0,1")

    def test_fixed_sequence_refold_cannot_be_l7_reference(self):
        refold = self.root / "refold.pdb"
        write_monomer(refold)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "length": len(SEQUENCE),
            "source_route": "route_C",
            "source_batch": "fixture",
            "cyclization_type": "head-to-tail_amide",
            "refold_pdb": str(refold),
            "refold_pdb_hash": hashlib.sha256(refold.read_bytes()).hexdigest(),
            "design_reference_pdb": str(refold),
            "design_reference_pdb_hash": hashlib.sha256(refold.read_bytes()).hexdigest(),
            "design_reference_role": "rfdiffusion_target_bound_backbone",
            "backbone_pdb": str(refold),
        }), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "cannot be used"):
            candidate_from_row({
                "candidate_id": "C0001",
                "sequence": SEQUENCE,
                "manifest_path": str(manifest),
            })

    def test_gpu_runner_preflight_rejects_missing_l7_reference(self):
        class Candidate:
            candidate_id = "C1250"
            design_reference_pdb = None

        with self.assertRaisesRegex(ContractError, "C1250"):
            require_design_references([Candidate()])

    def test_boltz_input_declares_cyclic_conditioning_and_covalent_bond(self):
        value = boltz_input_yaml(
            target_sequence="AAAA",
            binder_sequence=SEQUENCE,
            target_chain="A",
            binder_chain="B",
        )
        self.assertIn("cyclic: true", value)
        self.assertIn("atom1: [B, 8, C]", value)
        self.assertIn("atom2: [B, 1, N]", value)
        self.assertEqual(value.count("msa: empty"), 2)

    def test_rosetta_protocol_declares_cycle_before_interface_analysis(self):
        value = interface_xml(
            target_chain="A",
            binder_chain="B",
            binder_first_pose_index=86,
            binder_last_pose_index=93,
        )
        self.assertLess(value.index("declare_head_to_tail"), value.index("analyze_interface"))
        self.assertIn('res1="93" atom1="C"', value)
        self.assertIn('res2="86" atom2="N"', value)
        self.assertIn('interface="A_B"', value)
        self.assertIn('interface_sc="true"', value)
        self.assertIn('pack_input="true"', value)

    def test_post_relax_topology_declares_head_to_tail_bond(self):
        value = topology_xml(first_pose_index=1, last_pose_index=8)
        self.assertIn('<PeptideCyclizeMover name="cyclize_head_to_tail"', value)
        self.assertIn('res1="8" atom1="C"', value)
        self.assertIn('res2="1" atom2="N"', value)
        self.assertIn('rebuild_fold_tree="false"', value)

    def test_pyrosetta_scorefile_is_consumed_by_strict_parser(self):
        path = self.root / "pyrosetta_interface.sc"
        _write_scorefile(
            path,
            {"dSASA_int": 432.1, "sc_value": 0.67, "dG_separated": -8.4},
            "model",
        )
        self.assertEqual(
            parse_rosetta_interface_output(path.read_text(encoding="utf-8")),
            {"dsasa": 432.1, "sc": 0.67, "rosetta_dg_separated": -8.4},
        )

    def test_post_relax_pdb_footer_does_not_depend_on_output_directory(self):
        path = (self.root / "nested" / "post_relax.pdb").resolve()
        path.parent.mkdir()
        path.write_text(
            f"ATOM\n#BEGIN_POSE_ENERGIES_TABLE {path}\n"
            f"#END_POSE_ENERGIES_TABLE {path}\n",
            encoding="utf-8",
        )
        _normalize_pdb_embedded_path(path)
        value = path.read_text(encoding="utf-8")
        self.assertNotIn(str(path.parent), value)
        self.assertEqual(value.count("post_relax.pdb"), 2)

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

    def test_target_numbering_maps_predictor_ids_to_reviewed_pdb_ids(self):
        reference_path = self.root / "reviewed_target.pdb"
        reference, _ = chain_pdb(
            "AAA", "A", residue_numbers=[53, 92, 95]
        )
        reference_path.write_text(reference + "END\n", encoding="utf-8")

        prediction_path = self.root / "renumbered_complex.pdb"
        target, serial = chain_pdb(
            "AAA", "A", residue_numbers=[1, -476, -475]
        )
        binder, _ = chain_pdb(
            SEQUENCE, "B", shift=(0, 1.5, 0), start=serial
        )
        prediction_path.write_text(target + binder + "END\n", encoding="utf-8")

        reviewed = parse_pdb(reference_path)
        prediction = parse_pdb(prediction_path)
        numbers = canonical_target_residue_numbers(
            reviewed, "A", prediction, "A"
        )
        self.assertEqual(numbers, [53, 92, 95])
        interface = interface_hotspot_metrics(
            prediction, "A", "B", [53, 92, 95], 4.5,
            target_residue_numbers=numbers,
        )
        self.assertEqual(interface["covered_hotspots"], [53, 92, 95])
        self.assertEqual(interface["interface_target_residues"], [53, 92, 95])

        reference_complex_path = self.root / "reviewed_complex.pdb"
        reference_binder, _ = chain_pdb(
            SEQUENCE, "B", shift=(0, 1.5, 0), start=serial
        )
        reference_complex_path.write_text(
            reference + reference_binder + "END\n", encoding="utf-8"
        )
        self.assertLess(
            target_aligned_binder_rmsd(
                prediction,
                parse_pdb(reference_complex_path),
                "A", "B", "B",
            ),
            1e-10,
        )

    def test_pose_rmsd_aligns_reference_with_terminal_target_overhang(self):
        mobile_path = self.root / "mobile_overhang_complex.pdb"
        mobile_target, serial = chain_pdb(
            "AAA", "A", residue_numbers=[1, 2, 3]
        )
        mobile_binder, _ = chain_pdb(
            SEQUENCE, "B", shift=(0.0, 1.5, 0.0), start=serial
        )
        mobile_path.write_text(
            mobile_target + mobile_binder + "END\n", encoding="utf-8"
        )

        reference_path = self.root / "reference_overhang_complex.pdb"
        reference_target, serial = chain_pdb(
            "GAAA", "A", residue_numbers=[325, 326, 327, 328]
        )
        reference_binder, _ = chain_pdb(
            SEQUENCE, "B", shift=(1.2, 1.5, 0.0), start=serial
        )
        reference_path.write_text(
            reference_target + reference_binder + "END\n", encoding="utf-8"
        )

        self.assertLess(
            target_aligned_binder_rmsd(
                parse_pdb(mobile_path), parse_pdb(reference_path),
                "A", "B", "B",
            ),
            1e-10,
        )

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


