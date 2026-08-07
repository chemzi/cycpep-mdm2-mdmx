"""Pinned Boltz-2 adapter for independent cyclic peptide complex prediction."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np

from .adapters import run_command
from .contracts import ContractError, SEQUENCE_RE, file_sha256
from .protocol import PREDICTION_PROTOCOL
from .structures import exact_sequence_chain, parse_pdb, terminal_bond_distance


BOLTZ_VERSION = "2.2.1"
BOLTZ_MODEL_FAMILY = "Boltz-2"
BOLTZ_MODEL_ID = "boltz2_model_0"
BOLTZ2_CHECKPOINT_SHA256 = (
    "090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1"
)


def _validate_sequence(sequence: str, label: str) -> str:
    value = str(sequence or "").strip().upper()
    if not value or not SEQUENCE_RE.fullmatch(value):
        raise ContractError("boltz_sequence_invalid", f"invalid {label} sequence")
    return value


def _validate_chain(chain: str, label: str) -> str:
    value = str(chain or "").strip()
    if len(value) != 1 or not value.isalnum():
        raise ContractError(
            "boltz_chain_invalid", f"{label} must be one alphanumeric PDB chain ID"
        )
    return value


def boltz_input_yaml(
    *,
    target_sequence: str,
    binder_sequence: str,
    target_chain: str = "A",
    binder_chain: str = "B",
) -> str:
    """Build a safe Boltz YAML with cyclic conditioning and a real C--N bond."""
    target_sequence = _validate_sequence(target_sequence, "target")
    binder_sequence = _validate_sequence(binder_sequence, "binder")
    target_chain = _validate_chain(target_chain, "target_chain")
    binder_chain = _validate_chain(binder_chain, "binder_chain")
    if target_chain == binder_chain:
        raise ContractError("boltz_chain_collision", "target and binder chains must differ")
    return (
        "version: 1\n"
        "sequences:\n"
        "  - protein:\n"
        f"      id: {target_chain}\n"
        f"      sequence: {target_sequence}\n"
        "      msa: empty\n"
        "  - protein:\n"
        f"      id: {binder_chain}\n"
        f"      sequence: {binder_sequence}\n"
        "      msa: empty\n"
        "      cyclic: true\n"
        "constraints:\n"
        "  - bond:\n"
        f"      atom1: [{binder_chain}, {len(binder_sequence)}, C]\n"
        f"      atom2: [{binder_chain}, 1, N]\n"
    )


def _single_match(root: Path, pattern: str, label: str) -> Path:
    matches = sorted(path for path in root.rglob(pattern) if path.is_file())
    if len(matches) != 1:
        raise ContractError(
            "boltz_output_ambiguous",
            f"expected exactly one {label} matching {pattern}; found {matches}",
        )
    return matches[0]


def _installed_version(boltz_executable: Path, timeout: int) -> str:
    python = boltz_executable.parent / "python"
    if not python.is_file():
        raise ContractError(
            "boltz_python_missing",
            f"Boltz environment Python not found beside {boltz_executable}",
        )
    result = run_command(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('boltz'))",
        ],
        timeout=min(timeout, 60),
    )
    if result.exit_code:
        raise ContractError(
            "boltz_version_failed", f"cannot determine Boltz version: {result.stderr[-500:]}"
        )
    return result.stdout.strip()


def run_boltz_prediction(
    *,
    boltz_executable: str | Path,
    cache_dir: str | Path,
    checkpoint: str | Path,
    target_sequence: str,
    binder_sequence: str,
    output_dir: str | Path,
    target_chain: str = "A",
    binder_chain: str = "B",
    seed: int | None = None,
    diffusion_samples: int | None = None,
    timeout: int = 3600,
    no_kernels: bool = False,
) -> dict:
    """Run one pinned Boltz sample and normalize it to the Prediction contract."""
    if seed is None:
        # Fallback defaults come from the versioned protocol, not Magic Numbers.
        seed = PREDICTION_PROTOCOL["enrichment"]["seed_base"]
    if diffusion_samples is None:
        diffusion_samples = PREDICTION_PROTOCOL["boltz"]["diffusion_samples"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ContractError("seed_invalid", "Boltz seed must be an integer")
    if isinstance(diffusion_samples, bool) or not isinstance(diffusion_samples, int):
        raise ContractError(
            "boltz_diffusion_samples_invalid",
            "diffusion_samples must be an integer",
        )
    if diffusion_samples <= 0:
        raise ContractError(
            "boltz_diffusion_samples_invalid",
            "diffusion_samples must be a positive integer",
        )
    target_sequence = _validate_sequence(target_sequence, "target")
    binder_sequence = _validate_sequence(binder_sequence, "binder")
    target_chain = _validate_chain(target_chain, "target_chain")
    binder_chain = _validate_chain(binder_chain, "binder_chain")

    executable = Path(boltz_executable).expanduser().resolve()
    cache = Path(cache_dir).expanduser().resolve()
    checkpoint_path = Path(checkpoint).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not executable.is_file():
        raise ContractError("tool_unavailable", f"Boltz executable not found: {executable}")
    if not checkpoint_path.is_file():
        raise ContractError(
            "boltz_checkpoint_missing", f"Boltz checkpoint not found: {checkpoint_path}"
        )
    checkpoint_sha = file_sha256(checkpoint_path)
    if checkpoint_sha != BOLTZ2_CHECKPOINT_SHA256:
        raise ContractError(
            "boltz_checkpoint_hash_mismatch",
            f"unexpected Boltz-2 checkpoint SHA-256: {checkpoint_sha}",
        )
    version = _installed_version(executable, timeout)
    if version != BOLTZ_VERSION:
        raise ContractError(
            "boltz_version_mismatch",
            f"Boltz {BOLTZ_VERSION} is required; found {version}",
        )
    if destination.exists() and any(destination.iterdir()):
        raise ContractError(
            "predictor_output_exists", f"Boltz output directory is not empty: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    input_path = destination / "input.yaml"
    input_path.write_text(
        boltz_input_yaml(
            target_sequence=target_sequence,
            binder_sequence=binder_sequence,
            target_chain=target_chain,
            binder_chain=binder_chain,
        ),
        encoding="utf-8",
    )
    raw_dir = destination / "raw"
    command = [
        str(executable),
        "predict",
        str(input_path),
        "--out_dir",
        str(raw_dir),
        "--cache",
        str(cache),
        "--checkpoint",
        str(checkpoint_path),
        "--model",
        "boltz2",
        "--output_format",
        "pdb",
        "--write_full_pae",
        "--diffusion_samples",
        str(diffusion_samples),
        "--seed",
        str(seed),
        "--accelerator",
        "gpu",
        "--devices",
        "1",
    ]
    if no_kernels:
        command.append("--no_kernels")
    environment = os.environ.copy()
    environment["BOLTZ_CACHE"] = str(cache)
    result = run_command(command, timeout=timeout, env=environment)
    (destination / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (destination / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code:
        raise ContractError(
            "boltz_failed",
            f"Boltz exited {result.exit_code}; see {destination / 'stderr.log'}",
        )

    raw_pdb = _single_match(raw_dir, "input_model_0.pdb", "rank-0 PDB")
    raw_pae = _single_match(raw_dir, "pae_input_model_0.npz", "rank-0 PAE")
    raw_confidence = _single_match(
        raw_dir, "confidence_input_model_0.json", "rank-0 confidence JSON"
    )
    pdb_path = destination / "prediction.pdb"
    pae_path = destination / "pae.npz"
    shutil.copy2(raw_pdb, pdb_path)
    shutil.copy2(raw_pae, pae_path)

    structure = parse_pdb(pdb_path)
    if structure.sequence(target_chain) != target_sequence:
        raise ContractError(
            "boltz_target_sequence_drift",
            f"Boltz target chain {target_chain} does not match the requested sequence",
        )
    observed_binder_chain = exact_sequence_chain(structure, binder_sequence)
    if observed_binder_chain != binder_chain:
        raise ContractError(
            "boltz_binder_chain_drift",
            f"Boltz returned binder chain {observed_binder_chain}, expected {binder_chain}",
        )
    closure_distance = terminal_bond_distance(structure, binder_chain)
    if not np.isfinite(closure_distance) or closure_distance > 2.0:
        raise ContractError(
            "boltz_cyclic_bond_open",
            f"Boltz binder terminal C--N distance is {closure_distance:.3f} A",
        )
    with np.load(pae_path, allow_pickle=False) as payload:
        if "pae" not in payload:
            raise ContractError("pae_key_missing", f"Boltz PAE lacks 'pae': {pae_path}")
        pae = np.asarray(payload["pae"], dtype=float)
    expected_residues = len(target_sequence) + len(binder_sequence)
    if pae.shape != (expected_residues, expected_residues):
        raise ContractError(
            "boltz_pae_shape_mismatch",
            f"Boltz PAE shape {pae.shape} != {(expected_residues, expected_residues)}",
        )
    try:
        confidence = json.loads(raw_confidence.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError("boltz_confidence_malformed", str(raw_confidence)) from exc
    iptm = confidence.get("iptm")
    if not isinstance(iptm, (int, float)) or isinstance(iptm, bool):
        raise ContractError("boltz_iptm_missing", f"Boltz confidence lacks numeric ipTM")
    iptm = float(iptm)
    if not np.isfinite(iptm) or not 0 <= iptm <= 1:
        raise ContractError("boltz_iptm_invalid", f"invalid Boltz ipTM: {iptm}")

    metadata = {
        "tool": "Boltz",
        "tool_version": version,
        "model_family": BOLTZ_MODEL_FAMILY,
        "model_id": BOLTZ_MODEL_ID,
        "seed": seed,
        "requested_sequence": binder_sequence,
        "observed_sequence": structure.sequence(binder_chain),
        "target_sequence": target_sequence,
        "target_chain": target_chain,
        "binder_chain": binder_chain,
        "iptm": iptm,
        "confidence": confidence,
        "msa_mode": "single_sequence_explicit_empty",
        "cyclic_conditioning": True,
        "explicit_head_to_tail_bond": {
            "atom1": [binder_chain, len(binder_sequence), "C"],
            "atom2": [binder_chain, 1, "N"],
        },
        "terminal_c_to_n_distance_angstrom": closure_distance,
        "checkpoint_sha256": checkpoint_sha,
        "input_sha256": file_sha256(input_path),
        "command": command,
    }
    metadata_path = destination / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "predictor": "Boltz",
        "seed": seed,
        "primary": False,
        "pdb": str(pdb_path),
        "pdb_sha256": file_sha256(pdb_path),
        "pae": str(pae_path),
        "pae_sha256": file_sha256(pae_path),
        "metadata": str(metadata_path),
        "metadata_sha256": file_sha256(metadata_path),
        "binder_chain": binder_chain,
    }
