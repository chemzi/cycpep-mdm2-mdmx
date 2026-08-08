"""Shared PDB / metric fixtures for prediction tests (split from test_prediction_pipeline.py, PR8)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

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


def chain_pdb(
    sequence, chain, *, shift=(0.0, 0.0, 0.0), bfactor=90.0,
    start=1, residue_numbers=None,
):
    lines, serial = [], start
    for index, amino_acid in enumerate(sequence, 1):
        residue_number = (
            residue_numbers[index - 1] if residue_numbers is not None else index
        )
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
                residue_number,
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


