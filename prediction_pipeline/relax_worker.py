"""Topology-aware, provenance-bound PyRosetta post-relax adapter."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .adapters import run_command
from .protocol import PREDICTION_PROTOCOL
from .contracts import ContractError, file_sha256
from .rosetta_worker import PYROSETTA_VERSION
from .structures import (
    backbone_rmsd,
    exact_sequence_chain,
    parse_pdb,
    terminal_bond_distance,
)


POST_RELAX_PROTOCOL = (
    "peptide_cyclize_geometry_and_coordinate_constrained_fastrelax_ref2015"
)
POST_RELAX_TOOL = "PyRosetta FastRelax"
MAX_CYCLIC_BOND_DISTANCE_ANGSTROM = 2.0
MAX_POST_RELAX_BACKBONE_RMSD_ANGSTROM = 2.0
DEFAULT_COORDINATE_STDEV_ANGSTROM = 0.5
_ENRICHMENT_PROTOCOL = PREDICTION_PROTOCOL["parameters"]["enrichment"]


def topology_xml(*, first_pose_index: int, last_pose_index: int) -> str:
    if first_pose_index < 1 or last_pose_index <= first_pose_index:
        raise ContractError("relax_residue_index_invalid", "invalid cyclic pose indices")
    return f"""<ROSETTASCRIPTS>
  <MOVERS>
    <PeptideCyclizeMover name="cyclize_head_to_tail" />
    <DeclareBond name="refresh_head_to_tail_dependent_atoms"
      res1="{last_pose_index}" atom1="C"
      res2="{first_pose_index}" atom2="N"
      add_termini="true" rebuild_fold_tree="false" />
  </MOVERS>
  <PROTOCOLS>
    <Add mover="cyclize_head_to_tail" />
    <Add mover="refresh_head_to_tail_dependent_atoms" />
  </PROTOCOLS>
</ROSETTASCRIPTS>
"""


def _read_json_object(path: Path, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ContractError(code, f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(code, f"JSON artifact must contain an object: {path}")
    return value


def _finite_runtime_number(runtime: dict, key: str) -> float:
    try:
        value = float(runtime[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("post_relax_runtime_invalid", f"missing numeric {key}") from exc
    if not math.isfinite(value):
        raise ContractError("post_relax_runtime_invalid", f"non-finite {key}")
    return value


def run_post_relax(
    *,
    pyrosetta_python: str | Path,
    monomer_pdb: str | Path,
    sequence: str,
    cyclization_type: str,
    output_dir: str | Path,
    seed: int = _ENRICHMENT_PROTOCOL["post_relax_seed_base"],
    repeats: int = _ENRICHMENT_PROTOCOL["post_relax_repeats"],
    coordinate_stdev_angstrom: float = DEFAULT_COORDINATE_STDEV_ANGSTROM,
    timeout: int = 3600,
) -> dict:
    """Relax one cyclic monomer and return artifact entries for L4."""
    pyrosetta_python = Path(pyrosetta_python).expanduser().resolve()
    monomer_pdb = Path(monomer_pdb).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not pyrosetta_python.is_file():
        raise ContractError("tool_unavailable", f"PyRosetta Python not found: {pyrosetta_python}")
    if not monomer_pdb.is_file():
        raise ContractError("pdb_missing", f"monomer PDB not found: {monomer_pdb}")
    if cyclization_type.replace("-", "_") != "head_to_tail_amide":
        raise ContractError(
            "unsupported_cyclization",
            f"post-relax does not support {cyclization_type!r}",
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ContractError("post_relax_seed_invalid", "seed must be an integer")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ContractError("post_relax_repeats_invalid", "repeats must be a positive integer")
    if not math.isfinite(float(coordinate_stdev_angstrom)) or coordinate_stdev_angstrom <= 0:
        raise ContractError(
            "post_relax_constraint_invalid", "coordinate stdev must be positive"
        )
    if destination.exists() and any(destination.iterdir()):
        raise ContractError(
            "predictor_output_exists", f"post-relax directory is not empty: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)

    input_structure = parse_pdb(monomer_pdb)
    input_chain = exact_sequence_chain(input_structure, sequence.upper())
    if len(input_structure.chains) != 1:
        raise ContractError(
            "post_relax_input_not_monomer",
            f"post-relax input must contain exactly one chain: {list(input_structure.chains)}",
        )
    residues = input_structure.chains[input_chain]
    pre_distance = terminal_bond_distance(input_structure, input_chain)
    if pre_distance > MAX_CYCLIC_BOND_DISTANCE_ANGSTROM:
        raise ContractError(
            "post_relax_cyclic_bond_open",
            f"input terminal C--N distance is {pre_distance:.3f} A",
        )

    version_result = run_command(
        [
            str(pyrosetta_python), "-c",
            "import importlib.metadata; print(importlib.metadata.version('pyrosetta'))",
        ],
        timeout=60,
    )
    installed_version = version_result.stdout.strip()
    if version_result.exit_code or installed_version != PYROSETTA_VERSION:
        raise ContractError(
            "pyrosetta_version_mismatch",
            f"PyRosetta {PYROSETTA_VERSION} is required; found {installed_version!r}",
        )

    xml_path = destination / "cyclic_topology.xml"
    xml_path.write_text(
        topology_xml(first_pose_index=1, last_pose_index=len(residues)),
        encoding="utf-8",
    )
    output_pdb = destination / "post_relax.pdb"
    runtime_path = destination / "pyrosetta_runtime.json"
    command = [
        str(pyrosetta_python),
        str(Path(__file__).with_name("pyrosetta_relax_cli.py").resolve()),
        "--input-pdb", str(monomer_pdb),
        "--output-pdb", str(output_pdb),
        "--topology-xml", str(xml_path),
        "--runtime-metadata", str(runtime_path),
        "--expected-version", PYROSETTA_VERSION,
        "--sequence", sequence.upper(),
        "--first-residue", "1",
        "--last-residue", str(len(residues)),
        "--residue-count", str(len(residues)),
        "--seed", str(seed),
        "--repeats", str(repeats),
        "--coordinate-stdev", str(float(coordinate_stdev_angstrom)),
    ]
    result = run_command(command, timeout=timeout, cwd=destination)
    (destination / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (destination / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code:
        raise ContractError(
            "post_relax_failed",
            f"PyRosetta exited {result.exit_code}; see {destination / 'stderr.log'}",
        )
    if not output_pdb.is_file():
        raise ContractError("post_relax_output_missing", f"missing {output_pdb}")

    runtime = _read_json_object(runtime_path, "post_relax_runtime_invalid")
    if runtime.get("pyrosetta_package_version") != PYROSETTA_VERSION:
        raise ContractError("pyrosetta_version_mismatch", str(runtime_path))
    if runtime.get("requested_sequence") != sequence.upper() or runtime.get(
        "observed_sequence"
    ) != sequence.upper():
        raise ContractError("post_relax_sequence_drift", str(runtime_path))
    if (
        runtime.get("bond_applied_before_relax") is not True
        or runtime.get("bond_applied_before_virtual_cleanup") is not True
        or runtime.get("bond_applied_after_relax") is not True
    ):
        raise ContractError("post_relax_topology_missing", str(runtime_path))
    if runtime.get("topology_geometry_constraints_applied") is not True:
        raise ContractError("post_relax_topology_constraints_missing", str(runtime_path))
    virtual_removed = runtime.get("temporary_virtual_residues_removed")
    if isinstance(virtual_removed, bool) or not isinstance(virtual_removed, int):
        raise ContractError(
            "post_relax_runtime_invalid", "invalid virtual-residue cleanup count"
        )

    output_structure = parse_pdb(output_pdb)
    output_chain = exact_sequence_chain(output_structure, sequence.upper())
    if output_chain != input_chain or len(output_structure.chains) != 1:
        raise ContractError(
            "post_relax_chain_drift",
            f"input chain {input_chain!r}, output chains {list(output_structure.chains)}",
        )
    post_distance = terminal_bond_distance(output_structure, output_chain)
    if post_distance > MAX_CYCLIC_BOND_DISTANCE_ANGSTROM:
        raise ContractError(
            "post_relax_cyclic_bond_open",
            f"output terminal C--N distance is {post_distance:.3f} A",
        )
    drift = backbone_rmsd(output_structure, output_chain, input_structure, input_chain)
    if drift > MAX_POST_RELAX_BACKBONE_RMSD_ANGSTROM:
        raise ContractError(
            "post_relax_backbone_drift",
            f"post-relax backbone RMSD is {drift:.3f} A",
        )

    pre_score = _finite_runtime_number(runtime, "pre_total_score_ref2015")
    post_score = _finite_runtime_number(runtime, "post_total_score_ref2015")
    pre_constrained_score = _finite_runtime_number(
        runtime, "pre_total_score_with_constraints"
    )
    post_constrained_score = _finite_runtime_number(
        runtime, "post_total_score_with_constraints"
    )
    runtime_constraints = runtime.get("coordinate_constraints") or {}
    if runtime_constraints != {
        "enabled": True,
        "to_start_coordinates": True,
        "sidechains": False,
        "ramp_down": False,
        "stdev_angstrom": float(coordinate_stdev_angstrom),
    }:
        raise ContractError(
            "post_relax_constraint_invalid", "runtime coordinate constraints differ"
        )
    expected_constraint_weights = {
        "coordinate_constraint": 1.0,
        "atom_pair_constraint": 1.0,
        "angle_constraint": 1.0,
        "dihedral_constraint": 1.0,
    }
    if runtime.get("constraint_score_weights") != expected_constraint_weights:
        raise ContractError(
            "post_relax_constraint_invalid", "runtime score weights differ"
        )

    metadata = {
        "tool": POST_RELAX_TOOL,
        "tool_version": PYROSETTA_VERSION,
        "tool_version_output": str(runtime.get("pyrosetta_version") or "")[-2000:],
        "protocol": POST_RELAX_PROTOCOL,
        "input_pdb": str(monomer_pdb),
        "input_pdb_sha256": file_sha256(monomer_pdb),
        "output_pdb": str(output_pdb),
        "output_pdb_sha256": file_sha256(output_pdb),
        "sequence": sequence.upper(),
        "input_chain": input_chain,
        "output_chain": output_chain,
        "cyclization_type": "head_to_tail_amide",
        "bond_topology_applied": True,
        "topology_geometry_constraints_applied": True,
        "constraint_score_weights": expected_constraint_weights,
        "declared_bond": runtime.get("declared_bond"),
        "terminal_c_to_n_distance_pre_angstrom": pre_distance,
        "terminal_c_to_n_distance_post_angstrom": post_distance,
        "backbone_rmsd_to_input_angstrom": drift,
        "scorefunction": "ref2015",
        "pre_total_score_ref2015": pre_score,
        "post_total_score_ref2015": post_score,
        "score_delta_ref2015": post_score - pre_score,
        "pre_total_score_with_constraints": pre_constrained_score,
        "post_total_score_with_constraints": post_constrained_score,
        "seed": seed,
        "repeats": repeats,
        "coordinate_constraints": runtime_constraints,
        "move_map": runtime.get("move_map"),
        "design_enabled": runtime.get("design_enabled"),
        "applied_movers": runtime.get("applied_movers"),
        "temporary_virtual_residues_removed": virtual_removed,
        "topology_xml_sha256": file_sha256(xml_path),
        "runtime_metadata": str(runtime_path),
        "runtime_metadata_sha256": file_sha256(runtime_path),
        "command": command,
    }
    metadata_path = destination / "post_relax_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "post_relax_pdb": str(output_pdb),
        "post_relax_pdb_sha256": file_sha256(output_pdb),
        "post_relax_metadata": str(metadata_path),
        "post_relax_metadata_sha256": file_sha256(metadata_path),
        "metrics": {
            "nc_distance_pre": pre_distance,
            "nc_distance_post": post_distance,
            "backbone_rmsd": drift,
            "pre_total_score_ref2015": pre_score,
            "post_total_score_ref2015": post_score,
        },
    }
