"""Pinned PyRosetta runner for topology-aware cyclic-peptide FastRelax.

Only the Python standard library is imported before the pinned PyRosetta
package is checked.  The outer worker validates PDB geometry and writes the
auditable artifact metadata used by Prediction L4.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
from pathlib import Path


def _finite(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"non-finite {label}: {number}")
    return number


def run(args: argparse.Namespace) -> dict:
    version = importlib.metadata.version("pyrosetta")
    if version != args.expected_version:
        raise RuntimeError(
            f"PyRosetta {args.expected_version} is required; found {version}"
        )

    import pyrosetta
    from pyrosetta.rosetta.core.kinematics import MoveMap
    from pyrosetta.rosetta.protocols.relax import FastRelax
    from pyrosetta.rosetta.protocols.rosetta_scripts import XmlObjects

    pyrosetta.init(
        f"-mute all -constant_seed -jran {args.seed} "
        f"-relax:coord_cst_stdev {args.coordinate_stdev} "
        "-ignore_unrecognized_res false"
    )
    pose = pyrosetta.pose_from_file(str(Path(args.input_pdb).resolve()))
    if pose.total_residue() != args.residue_count:
        raise RuntimeError(
            f"expected {args.residue_count} residues; found {pose.total_residue()}"
        )
    requested_sequence = args.sequence.upper()
    if pose.sequence().upper() != requested_sequence:
        raise RuntimeError(
            f"input pose sequence {pose.sequence()!r} differs from {requested_sequence!r}"
        )

    xml_objects = XmlObjects.create_from_file(str(Path(args.topology_xml).resolve()))
    xml_objects.get_mover("declare_head_to_tail").apply(pose)
    bond_applied_before = bool(
        pose.residue(args.last_residue).is_bonded(pose.residue(args.first_residue))
    )
    if not bond_applied_before:
        raise RuntimeError("DeclareBond did not create the head-to-tail chemical bond")

    scorefxn = pyrosetta.create_score_function("ref2015")
    pre_score = _finite(scorefxn(pose), "pre-relax ref2015 score")

    move_map = MoveMap()
    move_map.set_bb(True)
    move_map.set_chi(True)
    move_map.set_jump(False)
    relax = FastRelax(scorefxn, args.repeats)
    relax.set_movemap(move_map)
    relax.set_enable_design(False)
    relax.constrain_coords(True)
    relax.constrain_relax_to_start_coords(True)
    relax.coord_constrain_sidechains(False)
    relax.ramp_down_constraints(False)
    relax.apply(pose)

    observed_sequence = pose.sequence().upper()
    if observed_sequence != requested_sequence:
        raise RuntimeError(
            f"FastRelax changed sequence to {observed_sequence!r}"
        )
    bond_applied_after = bool(
        pose.residue(args.last_residue).is_bonded(pose.residue(args.first_residue))
    )
    if not bond_applied_after:
        raise RuntimeError("head-to-tail chemical bond was lost during FastRelax")
    post_score = _finite(scorefxn(pose), "post-relax ref2015 score")
    output_pdb = Path(args.output_pdb).resolve()
    pose.dump_pdb(str(output_pdb))
    if not output_pdb.is_file() or output_pdb.stat().st_size == 0:
        raise RuntimeError(f"PyRosetta did not write {output_pdb}")

    result = {
        "pyrosetta_package_version": version,
        "pyrosetta_version": pyrosetta.version(),
        "requested_sequence": requested_sequence,
        "observed_sequence": observed_sequence,
        "residue_count": pose.total_residue(),
        "declared_bond": {
            "res1": args.last_residue,
            "atom1": "C",
            "res2": args.first_residue,
            "atom2": "N",
        },
        "bond_applied_before_relax": bond_applied_before,
        "bond_applied_after_relax": bond_applied_after,
        "scorefunction": "ref2015",
        "pre_total_score_ref2015": pre_score,
        "post_total_score_ref2015": post_score,
        "score_delta_ref2015": post_score - pre_score,
        "seed": args.seed,
        "repeats": args.repeats,
        "coordinate_constraints": {
            "enabled": True,
            "to_start_coordinates": True,
            "sidechains": False,
            "ramp_down": False,
            "stdev_angstrom": args.coordinate_stdev,
        },
        "move_map": {"backbone": True, "sidechains": True, "jumps": False},
        "design_enabled": False,
        "applied_movers": ["declare_head_to_tail", "FastRelax"],
    }
    Path(args.runtime_metadata).resolve().write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-pdb", required=True)
    parser.add_argument("--output-pdb", required=True)
    parser.add_argument("--topology-xml", required=True)
    parser.add_argument("--runtime-metadata", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--first-residue", required=True, type=int)
    parser.add_argument("--last-residue", required=True, type=int)
    parser.add_argument("--residue-count", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--repeats", required=True, type=int)
    parser.add_argument("--coordinate-stdev", required=True, type=float)
    return parser


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 2
    print(json.dumps({"status": "complete", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
