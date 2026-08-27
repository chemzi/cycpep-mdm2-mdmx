#!/usr/bin/env python3
"""Evaluate target-aligned pose recovery for reference-replay candidates."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.structures import (
    exact_sequence_chain,
    parse_pdb,
    target_aligned_binder_rmsd,
)


def _resolve(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def evaluate(artifacts_root: Path, candidate_ids: set[str]) -> dict:
    rows = [
        row for row in CandidateIndex.load()
        if row.get("source_route") == "benchmark_reference_replay"
        and (not candidate_ids or row.get("candidate_id") in candidate_ids)
    ]
    results = []
    for row in rows:
        candidate_id = row["candidate_id"]
        manifest_path = Path(row["manifest_path"]).expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reference = manifest.get("reference_metadata") or {}
        reference_path = _resolve(
            reference["reference_complex_pdb"], manifest_path.parent
        )
        if file_sha256(reference_path) != reference["reference_complex_sha256"]:
            raise ValueError(f"{candidate_id} reference complex hash mismatch")

        artifacts_path = artifacts_root / candidate_id / "artifacts.json"
        bundle = json.loads(artifacts_path.read_text(encoding="utf-8"))
        target_id = next(iter(bundle["targets"]))
        predictions = bundle["targets"][target_id]["complex_predictions"]
        reference_structure = parse_pdb(reference_path)
        samples = []
        for prediction in predictions:
            prediction_path = _resolve(prediction["pdb"], artifacts_path.parent)
            prediction_structure = parse_pdb(prediction_path)
            metadata_path = _resolve(prediction["metadata"], artifacts_path.parent)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            binder_chain = str(
                prediction.get("binder_chain")
                or metadata.get("binder_chain")
                or exact_sequence_chain(prediction_structure, row["sequence"])
            )
            samples.append({
                "predictor": prediction["predictor"],
                "model_id": metadata.get("model_id"),
                "seed": prediction["seed"],
                "pose_rmsd_angstrom": target_aligned_binder_rmsd(
                    prediction_structure,
                    reference_structure,
                    str(bundle["targets"][target_id]["target_chain"]),
                    binder_chain,
                    str(reference["reference_binder_chain"]),
                ),
            })
        values = [sample["pose_rmsd_angstrom"] for sample in samples]
        results.append({
            "candidate_id": candidate_id,
            "benchmark_id": reference["benchmark_id"],
            "sequence": row["sequence"],
            "samples": samples,
            "pose_rmsd_min_angstrom": min(values),
            "pose_rmsd_median_angstrom": statistics.median(values),
            "pose_recovered_below_2A": min(values) < 2.0,
        })
    recovered = sum(item["pose_recovered_below_2A"] for item in results)
    return {
        "schema_version": 1,
        "candidate_count": len(results),
        "pose_recovered_below_2A": recovered,
        "pose_recovery_rate": recovered / len(results) if results else None,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-root", required=True, type=Path)
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(
        args.artifacts_root.expanduser().resolve(), set(args.candidate)
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "complete",
        "candidate_count": result["candidate_count"],
        "pose_recovery_rate": result["pose_recovery_rate"],
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
