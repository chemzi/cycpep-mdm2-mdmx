#!/usr/bin/env python3
"""Register fixed-sequence experimental reference-replay candidates in Design."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.design import Design
from data_layer import CandidateIndex, EvidenceLogger
from peptide_contract import is_supported_cyclic_sequence
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.structures import exact_sequence_chain, parse_pdb


def _resolve(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def register(manifest_path: Path, *, batch_id: str, requested: set[str]) -> list[dict]:
    design = Design()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("project_id") != design.project_config.get("project_id"):
        raise ValueError(
            "benchmark/project mismatch: "
            f"{payload.get('project_id')!r} != "
            f"{design.project_config.get('project_id')!r}"
        )
    config = design.merge_config(
        {"target_id": payload["target_id"]},
        {"n": max(1, len(payload.get("candidates") or [])), "seed": 0},
    )
    output_root = Path(design.output_dir) / "benchmark_reference_replay" / batch_id
    existing = {
        (row.get("source_batch"), row.get("sequence")): row
        for row in CandidateIndex.load()
    }
    registered = []
    for entry in payload.get("candidates") or []:
        benchmark_id = str(entry.get("benchmark_id") or "").strip()
        if requested and benchmark_id not in requested:
            continue
        sequence = str(entry.get("sequence") or "").strip().upper()
        if not is_supported_cyclic_sequence(sequence):
            raise ValueError(f"unsupported benchmark sequence {benchmark_id}: {sequence}")
        duplicate = existing.get((batch_id, sequence))
        if duplicate:
            registered.append(duplicate)
            continue

        ligand_reference = _resolve(
            manifest_path.parent, entry["reference_ligand_pdb"]
        )
        complex_reference = _resolve(
            manifest_path.parent, entry["reference_complex_pdb"]
        )
        if file_sha256(ligand_reference) != entry["reference_ligand_sha256"]:
            raise ValueError(f"{benchmark_id} ligand reference hash mismatch")
        if file_sha256(complex_reference) != entry["reference_complex_sha256"]:
            raise ValueError(f"{benchmark_id} complex reference hash mismatch")
        ligand = parse_pdb(ligand_reference)
        if exact_sequence_chain(ligand, sequence) != entry["reference_binder_chain"]:
            raise ValueError(f"{benchmark_id} reference sequence/chain mismatch")

        candidate_id = design.next_candidate_id()
        candidate_dir = output_root / "candidates" / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        refold_pdb = candidate_dir / "refold.pdb"
        plddt = design.run_refold(sequence, str(refold_pdb))
        if plddt is None or not refold_pdb.is_file():
            raise RuntimeError(f"{benchmark_id} fixed-sequence refold failed")
        closure = design.ring_closure_check(
            str(refold_pdb), "head-to-tail_amide", sequence=sequence
        )
        if not closure.get("pass"):
            raise RuntimeError(
                f"{benchmark_id} refold failed closure contract: {closure}"
            )

        manifest = design.write_manifest(
            candidate_id,
            sequence,
            "benchmark_reference_replay",
            batch_id,
            str(refold_pdb),
            config,
            backbone_pdb=str(ligand_reference),
            cyclization="head-to-tail_amide",
            ring_closure=closure,
            design_reference_role="experimental_cyclic_peptide_structure",
            reference_metadata={
                "benchmark_id": benchmark_id,
                "benchmark_manifest": str(manifest_path),
                "reference_complex_pdb": str(complex_reference),
                "reference_complex_sha256": entry["reference_complex_sha256"],
                "reference_target_chain": entry["reference_target_chain"],
                "reference_binder_chain": entry["reference_binder_chain"],
                "source": entry.get("source") or {},
                "labels_withheld": bool(payload.get("labels_withheld")),
            },
        )
        candidate = design.candidate_from_manifest(
            manifest,
            plddt,
            notes={"benchmark_id": benchmark_id, "labels_withheld": True},
        )
        CandidateIndex.add(candidate)
        EvidenceLogger.log(
            "design",
            "benchmark_reference_candidate_registered",
            {"candidate": candidate, "benchmark_id": benchmark_id},
            targets=[payload["target_id"]],
            phase="design",
        )
        existing[(batch_id, sequence)] = candidate
        registered.append(candidate)
    return registered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--batch-id", default="keap1_reference_replay_v1")
    parser.add_argument("--benchmark", action="append", default=[])
    args = parser.parse_args()
    rows = register(
        args.manifest.expanduser().resolve(),
        batch_id=args.batch_id,
        requested=set(args.benchmark),
    )
    print(json.dumps({
        "status": "complete",
        "registered": [row["candidate_id"] for row in rows],
        "batch_id": args.batch_id,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
