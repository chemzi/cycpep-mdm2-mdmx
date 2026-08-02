"""Cyclic-topology-aware Rosetta InterfaceAnalyzer adapter."""

from __future__ import annotations

import json
from pathlib import Path

from .adapters import run_command
from .contracts import ContractError, file_sha256
from .metrics import parse_rosetta_interface_output
from .structures import exact_sequence_chain, parse_pdb, terminal_bond_distance


def interface_xml(
    *,
    target_chain: str,
    binder_chain: str,
    binder_first_pose_index: int,
    binder_last_pose_index: int,
) -> str:
    """Declare the macrocycle before computing Rosetta interface metrics."""
    if target_chain == binder_chain:
        raise ContractError("rosetta_chain_collision", "target and binder chains must differ")
    if binder_first_pose_index < 1 or binder_last_pose_index <= binder_first_pose_index:
        raise ContractError("rosetta_residue_index_invalid", "invalid binder pose indices")
    interface = f"{target_chain}_{binder_chain}"
    return f"""<ROSETTASCRIPTS>
  <SCOREFXNS>
    <ScoreFunction name="ref2015" weights="ref2015" />
  </SCOREFXNS>
  <MOVERS>
    <DeclareBond name="declare_head_to_tail"
      res1="{binder_last_pose_index}" atom1="C"
      res2="{binder_first_pose_index}" atom2="N"
      add_termini="true" rebuild_fold_tree="false" />
    <InterfaceAnalyzerMover name="analyze_interface"
      scorefxn="ref2015" interface="{interface}"
      pack_input="false" pack_separated="true" packstat="false"
      interface_sc="true" tracer="false" use_jobname="false" />
  </MOVERS>
  <PROTOCOLS>
    <Add mover="declare_head_to_tail" />
    <Add mover="analyze_interface" />
  </PROTOCOLS>
  <OUTPUT scorefxn="ref2015" />
</ROSETTASCRIPTS>
"""


def run_rosetta_interface(
    *,
    executable: str | Path,
    complex_pdb: str | Path,
    target_chain: str,
    binder_chain: str,
    binder_sequence: str,
    predictor: str,
    model_id: str,
    seed: int,
    output_dir: str | Path,
    timeout: int = 1800,
) -> dict:
    """Run InterfaceAnalyzer once and bind the result to one prediction PDB."""
    executable = Path(executable).expanduser().resolve()
    complex_pdb = Path(complex_pdb).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not executable.is_file():
        raise ContractError("tool_unavailable", f"Rosetta executable not found: {executable}")
    if not complex_pdb.is_file():
        raise ContractError("pdb_missing", f"complex PDB not found: {complex_pdb}")
    if destination.exists() and any(destination.iterdir()):
        raise ContractError(
            "predictor_output_exists", f"Rosetta output directory is not empty: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)

    structure = parse_pdb(complex_pdb)
    observed_chain = exact_sequence_chain(structure, binder_sequence)
    if observed_chain != binder_chain:
        raise ContractError(
            "rosetta_binder_chain_drift",
            f"declared binder chain {binder_chain} but observed {observed_chain}",
        )
    if target_chain not in structure.chains or target_chain == binder_chain:
        raise ContractError(
            "target_chain_mismatch", f"invalid Rosetta target chain {target_chain}"
        )
    closure_distance = terminal_bond_distance(structure, binder_chain)
    if closure_distance > 2.0:
        raise ContractError(
            "rosetta_cyclic_bond_open",
            f"input terminal C--N distance is {closure_distance:.3f} A",
        )
    binder_positions = [
        index for index, residue in enumerate(structure.residues, 1)
        if residue.chain == binder_chain
    ]
    if len(binder_positions) != len(binder_sequence):
        raise ContractError(
            "rosetta_binder_length_mismatch",
            "PDB binder residue count differs from requested sequence",
        )
    xml_path = destination / "cyclic_interface.xml"
    xml_path.write_text(
        interface_xml(
            target_chain=target_chain,
            binder_chain=binder_chain,
            binder_first_pose_index=binder_positions[0],
            binder_last_pose_index=binder_positions[-1],
        ),
        encoding="utf-8",
    )
    score_path = destination / "interface.sc"
    command = [
        str(executable),
        "-s",
        str(complex_pdb),
        "-parser:protocol",
        str(xml_path),
        "-out:file:score_only",
        str(score_path),
        "-overwrite",
    ]
    result = run_command(command, timeout=timeout, cwd=destination)
    (destination / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (destination / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code:
        raise ContractError(
            "rosetta_failed",
            f"Rosetta exited {result.exit_code}; see {destination / 'stderr.log'}",
        )
    if not score_path.is_file():
        raise ContractError("rosetta_output_missing", f"missing {score_path}")
    parsed = parse_rosetta_interface_output(
        score_path.read_text(encoding="utf-8", errors="replace")
    )
    version_result = run_command([str(executable), "-version"], timeout=60)
    version_text = (version_result.stdout or version_result.stderr).strip()
    metadata = {
        "tool": "RosettaScripts InterfaceAnalyzerMover",
        "tool_version_output": version_text[-2000:],
        "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
        "predictor": predictor,
        "model_id": model_id,
        "seed": seed,
        "prediction_pdb": str(complex_pdb),
        "prediction_pdb_sha256": file_sha256(complex_pdb),
        "target_chain": target_chain,
        "binder_chain": binder_chain,
        "binder_sequence": binder_sequence,
        "terminal_c_to_n_distance_angstrom": closure_distance,
        "declared_bond": {
            "res1": binder_positions[-1],
            "atom1": "C",
            "res2": binder_positions[0],
            "atom2": "N",
        },
        "scorefunction": "ref2015",
        "metrics": parsed,
        "xml_sha256": file_sha256(xml_path),
        "command": command,
    }
    metadata_path = destination / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "predictor": predictor,
        "model_id": model_id,
        "seed": seed,
        "prediction_pdb_sha256": file_sha256(complex_pdb),
        "output": str(score_path),
        "output_sha256": file_sha256(score_path),
        "metadata": str(metadata_path),
        "metadata_sha256": file_sha256(metadata_path),
    }
