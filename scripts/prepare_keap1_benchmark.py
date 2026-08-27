#!/usr/bin/env python3
"""Prepare chain-normalized KEAP1 benchmark structures from RCSB PDB files.

The script deliberately writes no affinity labels.  It produces only the
runtime inputs needed for reference replay and pose recovery.  Experimental
labels remain in a separate held-out file and are revealed after Prediction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prediction_pipeline.structures import exact_sequence_chain, parse_pdb


STRUCTURES = (
    ("7K2E", "A", "P", "GDEETGE"),
    ("7K2F", "A", "C", "GAEETGE"),
    ("7K2G", "B", "P", "GDEEAGE"),
    ("7K2H", "B", "P", "GDPETGE"),
    ("7K2I", "B", "P", "GAPETGE"),
    ("7K2M", "A", "P", "GEPETGE"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_records(path: Path, chain_map: dict[str, str]) -> list[str]:
    records: list[str] = []
    in_first_model = True
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = raw[:6].strip()
        if record == "MODEL":
            in_first_model = raw[10:14].strip() in {"", "1"}
            continue
        if record == "ENDMDL":
            if in_first_model:
                break
            continue
        if not in_first_model or record not in {"ATOM", "HETATM"}:
            continue
        if len(raw) < 22 or raw[21] not in chain_map:
            continue
        altloc = raw[16] if len(raw) > 16 else " "
        if altloc not in {" ", "A"}:
            continue
        normalized = f"{raw[:16]} {raw[17:21]}{chain_map[raw[21]]}{raw[22:]}"
        records.append(normalized)
    return records


def _write_structure(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records + ["END", ""]), encoding="utf-8")


def prepare(raw_root: Path, output_root: Path, manifest_path: Path) -> dict:
    candidates = []
    for pdb_id, target_chain, peptide_chain, sequence in STRUCTURES:
        source = raw_root / f"{pdb_id}.pdb"
        if not source.is_file():
            raise FileNotFoundError(f"missing RCSB structure: {source}")
        target_records = _selected_records(source, {target_chain: "A"})
        peptide_records = _selected_records(source, {peptide_chain: "B"})
        if not target_records or not peptide_records:
            raise ValueError(
                f"{pdb_id} expected target chain {target_chain} and peptide "
                f"chain {peptide_chain}"
            )

        complex_path = output_root / f"{pdb_id}_complex_AB.pdb"
        ligand_path = output_root / f"{pdb_id}_cyclic_peptide_B.pdb"
        _write_structure(complex_path, target_records + ["TER"] + peptide_records)
        _write_structure(ligand_path, peptide_records)

        observed_complex = parse_pdb(complex_path)
        observed_ligand = parse_pdb(ligand_path)
        if exact_sequence_chain(observed_complex, sequence) != "B":
            raise ValueError(f"{pdb_id} complex peptide sequence mismatch")
        if exact_sequence_chain(observed_ligand, sequence) != "B":
            raise ValueError(f"{pdb_id} ligand sequence mismatch")
        if "A" not in observed_complex.chains:
            raise ValueError(f"{pdb_id} normalized target chain A missing")

        candidates.append({
            "benchmark_id": pdb_id,
            "sequence": sequence,
            "reference_ligand_pdb": str(ligand_path.relative_to(manifest_path.parent)),
            "reference_ligand_sha256": _sha256(ligand_path),
            "reference_complex_pdb": str(complex_path.relative_to(manifest_path.parent)),
            "reference_complex_sha256": _sha256(complex_path),
            "reference_target_chain": "A",
            "reference_binder_chain": "B",
            "source": {
                "pdb_id": pdb_id,
                "doi": "10.1021/jacs.0c09799",
                "role": "experimental_positive_control",
            },
        })

    receptor_path = output_root / "7K2E_receptor_A.pdb"
    receptor_records = _selected_records(raw_root / "7K2E.pdb", {"A": "A"})
    _write_structure(receptor_path, receptor_records)
    payload = {
        "schema_version": 1,
        "benchmark_id": "keap1_jacs_2021_canonical_cyclic_series",
        "project_id": "keap1_cyclic_peptide_benchmark",
        "target_id": "KEAP1",
        "target_uniprot": "Q14145",
        "labels_withheld": True,
        "receptor_pdb": str(receptor_path.relative_to(manifest_path.parent)),
        "receptor_sha256": _sha256(receptor_path),
        "candidates": candidates,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    payload = prepare(
        args.raw_root.expanduser().resolve(),
        args.output_root.expanduser().resolve(),
        args.manifest.expanduser().resolve(),
    )
    print(json.dumps({
        "status": "complete",
        "benchmark_id": payload["benchmark_id"],
        "candidate_count": len(payload["candidates"]),
        "manifest": str(args.manifest.expanduser().resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
