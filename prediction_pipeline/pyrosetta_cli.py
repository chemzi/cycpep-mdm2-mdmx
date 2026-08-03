"""Small pinned PyRosetta runner used by the outer Prediction adapter.

This module deliberately uses only the Python standard library before loading
PyRosetta so it can run inside a dedicated, minimal PyRosetta environment.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
from pathlib import Path


REQUIRED_SCORE_KEYS = ("dSASA_int", "sc_value")


def _numeric_scores(values) -> dict[str, float]:
    result = {}
    for key, value in dict(values).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result[str(key)] = number
    return result


def _write_scorefile(path: Path, scores: dict[str, float], description: str) -> None:
    missing = [key for key in REQUIRED_SCORE_KEYS if key not in scores]
    if missing:
        raise RuntimeError(f"InterfaceAnalyzer omitted required scores: {missing}")
    ordered = ["dSASA_int", "sc_value"]
    if "dG_separated" in scores:
        ordered.append("dG_separated")
    ordered.append("description")
    values = [f"{scores[key]:.12g}" for key in ordered if key != "description"]
    values.append(description)
    path.write_text(
        f"SCORE: {' '.join(ordered)}\nSCORE: {' '.join(values)}\n",
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> dict:
    version = importlib.metadata.version("pyrosetta")
    if version != args.expected_version:
        raise RuntimeError(
            f"PyRosetta {args.expected_version} is required; found {version}"
        )

    import pyrosetta
    from pyrosetta.rosetta.protocols.rosetta_scripts import XmlObjects

    pyrosetta.init(
        f"-mute all -constant_seed -jran {args.seed} "
        "-ignore_unrecognized_res false"
    )
    pose = pyrosetta.pose_from_file(str(Path(args.pdb).resolve()))
    xml_objects = XmlObjects.create_from_file(str(Path(args.xml).resolve()))
    xml_objects.get_mover("declare_head_to_tail").apply(pose)
    xml_objects.get_mover("analyze_interface").apply(pose)
    scores = _numeric_scores(pose.scores)
    scorefile = Path(args.scorefile).resolve()
    _write_scorefile(scorefile, scores, args.description)

    result = {
        "pyrosetta_package_version": version,
        "pyrosetta_version": pyrosetta.version(),
        "applied_movers": ["declare_head_to_tail", "analyze_interface"],
        "scores": scores,
    }
    if args.runtime_metadata:
        Path(args.runtime_metadata).resolve().write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdb", required=True)
    parser.add_argument("--xml", required=True)
    parser.add_argument("--scorefile", required=True)
    parser.add_argument("--runtime-metadata")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--description", default="interface")
    parser.add_argument("--seed", required=True, type=int)
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
