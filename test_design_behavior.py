"""Design config / context / protocol behavior tests (split from test_design.py, PR8).

Covers output-dir resolution, hotspot-segment validation, Route C empty-guard,
seed coercion, DesignContext injection and versioned protocol binding.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agents.design import Design, DesignContext
from agents.design import config as design_config
from agents.design import route_c
from agents.design.config import (
    DESIGN_PROTOCOL,
    DESIGN_PROTOCOL_PATH,
    DESIGN_PROTOCOL_SHA256,
    _resolve_output_dir,
)
from agents.design.manifests import _write_manifest
from agents.design.service import _merge_config
from agents.design.validation import _pdb_residue_range
from core.protocol import protocol_identity_sha256
from project_config import load_project_config
from target_bootstrap import config_digest

AA1_TO_3 = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}


def pdb_atom(serial, atom_name, residue_name, chain, residue_number, xyz):
    x, y, z = xyz
    element = atom_name[0]
    return (
        f"ATOM  {serial:5d} {atom_name:>4s} {residue_name:>3s} "
        f"{chain}{residue_number:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}{1.00:6.2f}{0.00:6.2f}"
        f"          {element:>2s}  \n"
    )


def monomer_pdb(sequence, *, chain="A", nc_distance=1.33):
    lines, serial = [], 1
    for index, amino_acid in enumerate(sequence, 1):
        residue_name = AA1_TO_3[amino_acid]
        if index == 1:
            lines.append(pdb_atom(serial, "N", residue_name, chain, index, (0.0, 0.0, 0.0)))
            serial += 1
        lines.append(
            pdb_atom(
                serial, "CA", residue_name, chain, index,
                (20.0 + index * 3.0, 0.0, 0.0),
            )
        )
        serial += 1
        if index == len(sequence):
            lines.append(
                pdb_atom(serial, "C", residue_name, chain, index, (nc_distance, 0.0, 0.0))
            )
            serial += 1
    return "".join(lines)


def _write_segmented_pdb(path: Path, segments: list[tuple[int, int]]) -> None:
    lines = []
    serial = 1
    for start, end in segments:
        for residue in range(start, end + 1):
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA A{residue:4d}       "
                f"{residue}.000   2.000   3.000  1.00  0.00           C  \n"
            )
            serial += 1
    path.write_text("".join(lines), encoding="utf-8")


def _approved_project(target_pdb: Path) -> dict:
    config = load_project_config(raw={
        "project_id": "design_v5_mdm2_mdmx_test",
        "targets": [
            {
                "id": "MDM2",
                "structure": {
                    "pdb_id": "1YCR",
                    "chain": "A",
                    "coordinate_path": str(target_pdb),
                    "coordinate_sha256": hashlib.sha256(
                        target_pdb.read_bytes()
                    ).hexdigest(),
                },
                "binding_site": {"residues": [54, 93, 96], "status": "user_reviewed"},
                "design": {"lengths": [8, 9]},
            },
            {
                "id": "MDMX",
                "structure": {
                    "pdb_id": "3DAB",
                    "chain": "B",
                    "coordinate_path": str(target_pdb),
                    "coordinate_sha256": hashlib.sha256(
                        target_pdb.read_bytes()
                    ).hexdigest(),
                },
                "binding_site": {"residues": [53, 92, 95], "status": "user_reviewed"},
                "design": {"lengths": [8, 9]},
            },
        ],
    })
    config["review"] = {
        "status": "approved",
        "approved_digest": config_digest(config),
    }
    return config


class DesignOutputAndValidationTests(unittest.TestCase):
    def test_output_dir_resolution_skips_inaccessible_root(self):
        class _DeniedDamodelPath:
            def is_dir(self):
                raise PermissionError(13, "permission denied", "/root/damodel-tmp/novapeptide")

        with tempfile.TemporaryDirectory() as runner_temp:
            explicit_dir = Path(runner_temp) / "explicit-designs"
            self.assertEqual(
                _resolve_output_dir(
                    {"CYCPEP_DESIGN_ROOT": str(explicit_dir)}, _DeniedDamodelPath()
                ),
                explicit_dir,
            )
            resolved_ci = _resolve_output_dir(
                {"RUNNER_TEMP": runner_temp}, _DeniedDamodelPath()
            )
            self.assertEqual(resolved_ci, Path(runner_temp) / "novapeptide" / "designs")

    def test_pdb_residue_range_longest_segment_without_hotspots(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "two_seg.pdb"
            _write_segmented_pdb(pdb, [(1, 20), (100, 110)])
            self.assertEqual(_pdb_residue_range(str(pdb), "A"), (1, 20))

    def test_pdb_residue_range_hotspot_forces_its_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "two_seg.pdb"
            _write_segmented_pdb(pdb, [(1, 20), (100, 110)])
            self.assertEqual(
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[105]),
                (100, 110),
            )
            self.assertEqual(
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[10, 15]),
                (1, 20),
            )

    def test_pdb_residue_range_spanning_hotspots_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "two_seg.pdb"
            _write_segmented_pdb(pdb, [(1, 20), (100, 110)])
            with self.assertRaises(ValueError):
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[10, 105])

    def test_pdb_residue_range_absent_hotspot_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "two_seg.pdb"
            _write_segmented_pdb(pdb, [(1, 20), (100, 110)])
            with self.assertRaises(ValueError):
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[999])

    def test_pdb_residue_range_isolated_hotspot_own_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "gap.pdb"
            _write_segmented_pdb(pdb, [(1, 5), (80, 80), (150, 155)])
            self.assertEqual(
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[80]),
                (80, 80),
            )

    def test_pdb_residue_range_single_segment_with_hotspots(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdb = Path(tmp) / "single.pdb"
            _write_segmented_pdb(pdb, [(50, 70)])
            self.assertEqual(
                _pdb_residue_range(str(pdb), "A", hotspot_residues=[53, 68]),
                (50, 70),
            )

    def test_route_c_empty_base_combos_guard_present(self):
        source = Path("agents/design/route_c.py").read_text(encoding="utf-8")
        self.assertIn("if not base_combos:", source)
        self.assertIn("route_c_empty", source)


class DesignConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_config = design_config.ACTIVE_PROJECT_CONFIG
        cls.root = Path(tempfile.mkdtemp(prefix="design-config-test-"))
        cls.target_pdb = cls.root / "target.pdb"
        cls.target_pdb.write_text(
            "ATOM      1  CA  ALA B   1       1.000   2.000   3.000  1.00  0.00           C  \n",
            encoding="utf-8",
        )
        cls.project = _approved_project(cls.target_pdb)
        design_config.ACTIVE_PROJECT_CONFIG = cls.project

    @classmethod
    def tearDownClass(cls):
        design_config.ACTIVE_PROJECT_CONFIG = cls._original_config

    def test_seed_type_coercion_and_range_validation(self):
        coerced = _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": "42"})
        self.assertEqual(coerced["seed"], 42)
        self.assertIsInstance(coerced["seed"], int)
        self.assertEqual(
            _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": 42.0})["seed"],
            42,
        )
        with self.assertRaises(ValueError):
            _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": 42.9})
        with self.assertRaises(ValueError):
            _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": -1})
        with self.assertRaises(ValueError):
            _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": 2**31})
        self.assertEqual(
            _merge_config({"target_name": "3DAB", "chain": "B"}, {"seed": 0})["seed"],
            0,
        )

    def test_design_context_injection(self):
        custom_cfg = dict(self.project)
        custom_cfg["project_id"] = "custom_injected_project"
        custom_cfg["review"] = {
            "status": "approved",
            "approved_digest": config_digest(custom_cfg),
        }
        custom_ctx = DesignContext(
            project_config=custom_cfg,
            output_dir=str(Path(tempfile.mkdtemp(prefix="design-ctx-test-")) / "designs"),
        )
        design = Design(context=custom_ctx)
        self.assertIs(design.context, custom_ctx)
        self.assertTrue(str(custom_ctx.output_dir).endswith("designs"))
        self.assertIs(
            Design().context.project_config,
            design_config.ACTIVE_PROJECT_CONFIG,
        )

        original = route_c.design_atsp_derived
        try:
            captured = {}
            def _fake_route_c(target_spec=None, design_config=None, context=None):
                captured["context"] = context
                return ["candidate-from-context"]

            route_c.design_atsp_derived = _fake_route_c
            self.assertEqual(
                design.design_atsp_derived(design_config={"n": 5}),
                ["candidate-from-context"],
            )
            self.assertIs(captured["context"], custom_ctx)
        finally:
            route_c.design_atsp_derived = original

        merged = _merge_config(
            {"target_name": "MDM2"}, {"seed": 7}, project_config=custom_cfg
        )
        self.assertEqual(merged["project_id"], "custom_injected_project")
        self.assertEqual(merged["seed"], 7)

    def test_versioned_protocol_binding(self):
        self.assertTrue(DESIGN_PROTOCOL_PATH.is_file())
        self.assertEqual(DESIGN_PROTOCOL["version"], "1.0")
        self.assertEqual(
            DESIGN_PROTOCOL["parameters"]["ligandmpnn"]["n_seq_per_backbone"], 8
        )
        self.assertEqual(
            DESIGN_PROTOCOL["parameters"]["mutation"]["attempts_factor"], 10
        )
        self.assertEqual(
            DESIGN_PROTOCOL["parameters"]["mutation"]["protected_pharmacophore"],
            "FWL",
        )
        self.assertEqual(
            json.loads(DESIGN_PROTOCOL_PATH.read_text(encoding="utf-8")),
            DESIGN_PROTOCOL,
        )
        self.assertEqual(len(DESIGN_PROTOCOL_SHA256), 64)
        self.assertEqual(
            DESIGN_PROTOCOL_SHA256,
            protocol_identity_sha256(
                DESIGN_PROTOCOL["name"],
                DESIGN_PROTOCOL["version"],
                DESIGN_PROTOCOL["parameters"],
            ),
        )

    def test_manifest_records_protocol_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            refold_pdb = Path(tmp) / "refold.pdb"
            refold_pdb.write_text(monomer_pdb("ACDEFGHI", nc_distance=1.33), encoding="utf-8")
            cfg = {"target_name": "1YCR", "target_pdb": "/tmp/test.pdb", "seed": 42}
            manifest = _write_manifest(
                "C9001", "ACDEFGHI", "route_C_test", "batch_proto", str(refold_pdb), cfg
            )
            self.assertEqual(manifest["protocol_version"], DESIGN_PROTOCOL["version"])
            self.assertEqual(manifest["protocol_sha256"], DESIGN_PROTOCOL_SHA256)

    def test_route_sources_read_protocol_not_magic_numbers(self):
        route_a_src = Path("agents/design/route_a.py").read_text(encoding="utf-8")
        route_c_src = Path("agents/design/route_c.py").read_text(encoding="utf-8")
        self.assertIn("n_seq_per_backbone", route_a_src)
        self.assertIn("attempts_factor", route_c_src)
        self.assertIn("protected_pharmacophore", route_c_src)


if __name__ == "__main__":
    unittest.main()
