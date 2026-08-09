"""Assemble the KEAP1 calibration control manifest (v3 P0-C D1).

Combines the six published experimental positives (PDB 7K2E-7K2M, DOI
10.1021/jacs.0c09799) with deterministic in-silico sequence-permutation
negatives and writes ``control_manifest_v2.json``.

The negatives are decoys, not experimentally validated non-binders; they are
labelled ``in_silico_sequence_negative_control`` and their provenance records
the originating positive.  The team must confirm the negative set before using
the resulting dataset for production calibration.

Generation is deterministic: the same inputs and seed always produce the same
manifest, so calibration results are reproducible without any hash machinery.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

NEGATIVE_ROLE = "in_silico_sequence_negative_control"
DEFAULT_SEED = 20260809


def _scramble(sequence: str, rng: random.Random, attempts: int = 50) -> str:
    """Return a permutation of ``sequence`` that differs in every position."""
    letters = list(sequence)
    if len(letters) < 2:
        return sequence
    for _ in range(attempts):
        candidate = letters[:]
        rng.shuffle(candidate)
        if all(a != b for a, b in zip(letters, candidate)):
            return "".join(candidate)
    return "".join(candidate)


def build_manifest(
    positives_path: Path,
    *,
    negatives_per_positive: int,
    seed: int,
) -> dict:
    payload = json.loads(positives_path.read_text(encoding="utf-8"))
    positives = [item for item in payload.get("controls", []) if isinstance(item, dict)]
    if not positives:
        raise ValueError("positive control manifest contains no controls")
    rng = random.Random(seed)
    controls = []
    for positive in positives:
        controls.append(dict(positive))
        sequence = positive.get("sequence", "")
        source = positive.get("source") or {}
        origin_pdb = source.get("pdb_id")
        for index in range(negatives_per_positive):
            scrambled = _scramble(sequence, rng)
            if scrambled == sequence:
                continue
            controls.append({
                "control_id": f"{positive.get('control_id')}-negative-{index + 1}",
                "label": "negative",
                "role": NEGATIVE_ROLE,
                "sequence": scrambled,
                "source": {
                    "pdb_id": origin_pdb,
                    "method": "deterministic in-silico sequence permutation",
                    "note": "not experimentally validated as non-binder; team must confirm",
                },
            })
    return {
        "schema_version": 2,
        "manifest_kind": "keap1_control_manifest",
        "benchmark_id": payload.get("benchmark_id"),
        "target_id": payload.get("target_id"),
        "target_uniprot": payload.get("target_uniprot"),
        "negatives_per_positive": negatives_per_positive,
        "seed": seed,
        "controls": controls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="build KEAP1 calibration control manifest")
    parser.add_argument(
        "--positives",
        default="benchmarks/keap1/calibration/positive_controls.json",
        help="positive control manifest (schema v2)",
    )
    parser.add_argument("--negatives-per-positive", type=int, default=3)
    parser.add_argument(
        "--output",
        default="benchmarks/keap1/calibration/control_manifest_v2.json",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = build_manifest(
        Path(args.positives),
        negatives_per_positive=args.negatives_per_positive,
        seed=args.seed,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output),
        "n_positive": sum(1 for c in manifest["controls"] if c["label"] == "positive"),
        "n_negative": sum(1 for c in manifest["controls"] if c["label"] == "negative"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
