"""Pinned, fixed-sequence ColabDesign worker.

Run this module inside the GPU environment.  It performs prediction only;
there is no sequence-design stage.  The requested sequence is checked against
both ColabDesign's hard sequence tensor and the emitted PDB before success is
reported.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .contracts import ContractError, SEQUENCE_RE, file_sha256
from .structures import exact_sequence_chain, parse_pdb
from peptide_contract import (
    MAX_CYCLIC_PEPTIDE_LENGTH,
    MIN_CYCLIC_PEPTIDE_LENGTH,
)


def _git_head(repository: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ContractError(
            "colabdesign_version_unavailable",
            f"cannot resolve ColabDesign git revision at {repository}",
        ) from exc
    head = result.stdout.strip()
    if result.returncode or len(head) != 40:
        raise ContractError(
            "colabdesign_version_unavailable",
            f"cannot resolve ColabDesign git revision at {repository}: {result.stderr}",
        )
    return head


def _assert_clean_checkout(repository: Path) -> None:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repository), "status", "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ContractError(
            "colabdesign_version_unavailable",
            f"cannot inspect ColabDesign checkout at {repository}",
        ) from exc
    if result.returncode or result.stdout.strip():
        raise ContractError(
            "colabdesign_checkout_dirty",
            f"tracked ColabDesign sources are modified at {repository}",
        )


def _assert_cyclic_offset_supported(repository: Path, use_multimer: bool) -> None:
    module_name = "modules_multimer.py" if use_multimer else "modules.py"
    path = repository / "colabdesign" / "af" / "alphafold" / "model" / module_name
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(
            "cyclic_offset_backend_missing", f"cannot inspect {path}"
        ) from exc
    if (
        '"offset" in batch' not in source
        and "'offset' in batch" not in source
    ):
        raise ContractError(
            "cyclic_offset_backend_unsupported",
            f"{module_name} does not consume the pairwise offset feature",
        )


def _cyclic_offset(length: int) -> np.ndarray:
    indices = np.arange(length)
    doubled = np.stack([indices, indices + length], axis=-1)
    linear = indices[:, None] - indices[None, :]
    cyclic = np.abs(
        doubled[:, None, :, None] - doubled[None, :, None, :]
    ).min(axis=(2, 3))
    replace = cyclic < np.abs(linear)
    cyclic[replace] = -cyclic[replace]
    return cyclic * np.sign(linear)


def _apply_cyclic_offset(model, start: int, length: int) -> None:
    if "offset" not in model._inputs:
        indices = np.asarray(model._inputs["residue_index"])
        model._inputs["offset"] = indices[:, None] - indices[None, :]
    offset = np.asarray(model._inputs["offset"]).copy()
    end = start + length
    if offset.shape[0] < end or offset.shape[1] < end:
        raise ContractError(
            "colabdesign_offset_shape",
            f"offset shape {offset.shape} cannot contain cyclic slice {start}:{end}",
        )
    offset[start:end, start:end] = _cyclic_offset(length)
    model._inputs["offset"] = offset


def _normalize_complex_target_chain(
    pdb_path: Path, binder_sequence: str, requested_target_chain: str
) -> tuple[str, str]:
    """Restore the reviewed target chain after ColabDesign renumbers it.

    ColabDesign's binder protocol emits target/binder as A/B regardless of the
    source PDB chain.  Downstream provenance, hotspot numbering, PRODIGY and
    Rosetta all use the reviewed chain identifier, so normalize the emitted
    coordinate artifact before it is hashed or registered.
    """
    requested = str(requested_target_chain or "").strip()
    if len(requested) != 1:
        raise ContractError(
            "target_chain_invalid", "PDB target chain must be one character"
        )
    structure = parse_pdb(pdb_path)
    binder_chain = exact_sequence_chain(structure, binder_sequence)
    target_chains = sorted(set(structure.chains) - {binder_chain})
    if len(target_chains) != 1:
        raise ContractError(
            "target_chain_ambiguous",
            f"expected one predicted target chain, observed {target_chains}",
        )
    observed = target_chains[0]
    if requested == binder_chain:
        raise ContractError(
            "target_chain_collision",
            f"requested target chain {requested} collides with binder chain",
        )
    if observed != requested:
        records = {"ATOM  ", "HETATM", "ANISOU", "TER   "}
        rewritten = []
        for line in pdb_path.read_text(encoding="utf-8").splitlines(keepends=True):
            if len(line) > 21 and line[:6] in records and line[21] == observed:
                line = f"{line[:21]}{requested}{line[22:]}"
            rewritten.append(line)
        pdb_path.write_text("".join(rewritten), encoding="utf-8", newline="")
        structure = parse_pdb(pdb_path)
        if requested not in structure.chains or observed in structure.chains:
            raise ContractError(
                "target_chain_normalization_failed",
                f"could not normalize predicted chain {observed} to {requested}",
            )
        binder_chain = exact_sequence_chain(structure, binder_sequence)
    return requested, binder_chain


def run(args: argparse.Namespace) -> dict:
    sequence = args.sequence.strip().upper()
    if not SEQUENCE_RE.fullmatch(sequence) or not (
        MIN_CYCLIC_PEPTIDE_LENGTH <= len(sequence) <= MAX_CYCLIC_PEPTIDE_LENGTH
    ):
        raise ContractError(
            "sequence_invalid",
            f"sequence must be {MIN_CYCLIC_PEPTIDE_LENGTH}-"
            f"{MAX_CYCLIC_PEPTIDE_LENGTH} standard amino acids",
        )
    colabdesign_dir = Path(args.colabdesign_dir).expanduser().resolve()
    observed_commit = _git_head(colabdesign_dir)
    if observed_commit != args.expected_commit:
        raise ContractError(
            "colabdesign_commit_mismatch",
            f"expected {args.expected_commit}, observed {observed_commit}",
        )
    _assert_clean_checkout(colabdesign_dir)
    _assert_cyclic_offset_supported(
        colabdesign_dir, use_multimer=bool(args.target_pdb and args.use_multimer)
    )

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pdb_path = output_dir / "prediction.pdb"
    pae_path = output_dir / "pae.npz"
    metadata_path = output_dir / "metadata.json"

    sys.path.insert(0, str(colabdesign_dir))
    from colabdesign import clear_mem, mk_af_model  # type: ignore

    model = None
    try:
        if args.target_pdb:
            model = mk_af_model(
                protocol="binder",
                use_multimer=args.use_multimer,
                data_dir=str(Path(args.data_dir).expanduser().resolve()),
            )
            model.prep_inputs(
                pdb_filename=str(Path(args.target_pdb).expanduser().resolve()),
                target_chain=args.target_chain,
                binder_len=len(sequence),
                rm_target_seq=False,
                rm_binder_seq=True,
            )
            cyclic_start = int(model._target_len)
            protocol = "binder"
        else:
            model = mk_af_model(
                protocol="hallucination",
                data_dir=str(Path(args.data_dir).expanduser().resolve()),
            )
            model.prep_inputs(length=len(sequence))
            cyclic_start = 0
            protocol = "hallucination"

        model.restart(seed=args.seed, seq=sequence)
        _apply_cyclic_offset(model, cyclic_start, len(sequence))
        aux = model.predict(
            seq=sequence,
            seed=args.seed,
            models=[args.model_number],
            num_models=1,
            num_recycles=args.num_recycles,
            sample_models=False,
            dropout=False,
            hard=True,
            soft=False,
            verbose=False,
            return_aux=True,
        )
        observed_sequences = model.get_seq(get_best=False)
        if observed_sequences != [sequence]:
            raise ContractError(
                "colabdesign_sequence_drift",
                f"requested {[sequence]}, observed {observed_sequences}",
            )

        model.save_pdb(str(pdb_path), get_best=False, aux=aux)
        structure = parse_pdb(pdb_path)
        binder_chain = exact_sequence_chain(structure, sequence)
        normalized_target_chain = args.target_chain
        if args.target_pdb:
            normalized_target_chain, binder_chain = _normalize_complex_target_chain(
                pdb_path, sequence, args.target_chain
            )
        pae = np.asarray(aux["pae"], dtype=float)
        plddt = np.asarray(aux["plddt"], dtype=float)
        if pae.ndim != 2 or pae.shape[0] != pae.shape[1]:
            raise ContractError("colabdesign_pae_shape", f"unexpected PAE shape {pae.shape}")
        np.savez_compressed(pae_path, pae=pae, plddt=plddt)

        metadata = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool": "ColabDesign",
            "tool_commit": observed_commit,
            "model_family": "AlphaFold2",
            "model_variant": "multimer" if args.use_multimer else "monomer",
            "model_id": f"alphafold2_model_{args.model_number}",
            "protocol": protocol,
            "requested_sequence": sequence,
            "observed_sequence": observed_sequences[0],
            "binder_chain": binder_chain,
            "target_chain": normalized_target_chain,
            "seed": args.seed,
            "model_number": args.model_number,
            "num_recycles": args.num_recycles,
            "use_multimer": bool(args.use_multimer),
            "cyclic_offset_enabled": True,
            "cyclic_offset_slice": [cyclic_start, cyclic_start + len(sequence)],
            "plddt_mean_raw": float(plddt.mean()),
            "iptm": float(aux["i_ptm"]) if "i_ptm" in aux else None,
            "ptm": float(aux["ptm"]) if "ptm" in aux else None,
            "pdb": str(pdb_path),
            "pdb_sha256": file_sha256(pdb_path),
            "pae": str(pae_path),
            "pae_sha256": file_sha256(pae_path),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return metadata
    finally:
        if model is not None:
            clear_mem()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--colabdesign-dir", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--model-number", type=int, default=0)
    parser.add_argument("--num-recycles", type=int, default=3)
    parser.add_argument("--target-pdb")
    parser.add_argument("--target-chain")
    parser.add_argument("--use-multimer", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if bool(args.target_pdb) != bool(args.target_chain):
        raise SystemExit("--target-pdb and --target-chain must be supplied together")
    try:
        metadata = run(args)
    except ContractError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": str(exc)}))
        return 2
    print(json.dumps({"status": "complete", "metadata": metadata}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
