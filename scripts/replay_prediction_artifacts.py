"""Replay preserved real Prediction artifacts against a Design handoff locally.

This utility makes a self-contained, immutable regression workspace.  It never
invents a Design backbone: when the original backbone was not preserved, L7 is
expected to remain pending.  Absolute server paths in a preserved artifacts.json
are remapped to files below the supplied candidate-artifact directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_layer  # noqa: E402
from data_layer import CandidateIndex, State  # noqa: E402
from prediction_pipeline import PredictionConfig, PredictionPipeline  # noqa: E402
from prediction_pipeline.contracts import (  # noqa: E402
    ContractError,
    file_sha256,
    object_sha256,
)
from project_config import load_project_config  # noqa: E402


FILE_KEYS = (
    "pdb",
    "pae",
    "metadata",
    "post_relax_pdb",
    "post_relax_metadata",
    "design_reference_pdb",
    "prodigy_output",
    "rosetta_output",
)


def _candidate_row(design_product: Path, candidate_id: str) -> dict:
    path = design_product / "candidate_index.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    matches = [row for row in rows if row.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise ContractError(
            "replay_candidate_missing",
            f"expected one {candidate_id} row in {path}; found {len(matches)}",
        )
    return matches[0]


def _source_relative_path(raw: str, source_root: Path, candidate_id: str) -> Path:
    value = Path(str(raw)).expanduser()
    if not value.is_absolute():
        relative = value
    else:
        positions = [index for index, part in enumerate(value.parts) if part == candidate_id]
        if not positions:
            raise ContractError(
                "replay_path_unmapped",
                f"absolute artifact path does not contain {candidate_id}: {value}",
            )
        relative = Path(*value.parts[positions[-1] + 1:])
    source = (source_root / relative).resolve()
    try:
        source.relative_to(source_root)
    except ValueError as exc:
        raise ContractError(
            "replay_path_escape", f"artifact escapes source directory: {raw}"
        ) from exc
    if not source.is_file():
        raise ContractError("replay_artifact_missing", f"artifact not preserved: {source}")
    return relative


def _rewrite_file_paths(value, source_root: Path, candidate_id: str):
    if isinstance(value, list):
        for item in value:
            _rewrite_file_paths(item, source_root, candidate_id)
        return
    if not isinstance(value, dict):
        return
    for key, item in list(value.items()):
        if key in FILE_KEYS and isinstance(item, str) and item.strip():
            value[key] = _source_relative_path(
                item, source_root, candidate_id
            ).as_posix()
        else:
            _rewrite_file_paths(item, source_root, candidate_id)


def _target_mapping(values: list[str]) -> dict[str, Path]:
    result = {}
    for raw in values:
        target_id, separator, path = raw.partition("=")
        target_id = target_id.strip()
        coordinate = Path(path).expanduser().resolve()
        if not separator or not target_id or not coordinate.is_file():
            raise ContractError(
                "replay_target_invalid", f"--target must be ID=/existing/file.pdb: {raw}"
            )
        if target_id in result:
            raise ContractError("replay_target_duplicate", f"duplicate target {target_id}")
        result[target_id] = coordinate
    return result


def _approved_project(targets: dict[str, Path]) -> dict:
    project = load_project_config()
    expected = {target["id"] for target in project["targets"] if target.get("required", True)}
    if set(targets) != expected:
        raise ContractError(
            "replay_targets_incomplete",
            f"expected target coordinates for {sorted(expected)}; received {sorted(targets)}",
        )
    for target in project["targets"]:
        coordinate = targets[target["id"]]
        target.setdefault("structure", {}).update({
            "coordinate_path": str(coordinate),
            "coordinate_sha256": file_sha256(coordinate),
        })
    content = json.loads(json.dumps(project))
    content.pop("review", None)
    digest = object_sha256(content)
    project["review"] = {
        "status": "approved",
        "revision": int((project.get("review") or {}).get("revision", 1)),
        "approved_digest": digest,
        "content_digest": digest,
        "blocking_issues": [],
        "warnings": ["local replay using preserved reviewed target coordinates"],
    }
    return project


def run(args: argparse.Namespace) -> dict:
    design_product = Path(args.design_product).expanduser().resolve()
    source_root = Path(args.source_candidate_artifacts).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    if output_root.exists():
        raise ContractError(
            "replay_output_exists",
            f"refusing to overwrite existing replay workspace: {output_root}",
        )
    if source_root.name != args.candidate:
        raise ContractError(
            "replay_source_candidate_mismatch",
            f"source directory must be named {args.candidate}: {source_root}",
        )

    row = _candidate_row(design_product, args.candidate)
    source_bundle_path = source_root / "artifacts.json"
    source_bundle = json.loads(source_bundle_path.read_text(encoding="utf-8"))
    if source_bundle.get("candidate_id") != args.candidate:
        raise ContractError("replay_bundle_mismatch", "artifact bundle candidate mismatch")
    if str(source_bundle.get("sequence") or "").upper() != row["sequence"].upper():
        raise ContractError("replay_bundle_mismatch", "artifact bundle sequence mismatch")
    _rewrite_file_paths(source_bundle, source_root, args.candidate)

    artifact_root = output_root / "artifacts"
    candidate_artifacts = artifact_root / args.candidate
    candidate_artifacts.parent.mkdir(parents=True)
    shutil.copytree(source_root, candidate_artifacts)
    bundle_path = candidate_artifacts / "artifacts.json"
    bundle_path.write_text(
        json.dumps(source_bundle, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    monomers = source_bundle.get("global", {}).get("monomer_predictions") or []
    if not monomers:
        raise ContractError("replay_monomer_missing", "no preserved monomer prediction")
    primary = sorted(
        monomers,
        key=lambda item: (not bool(item.get("primary")), int(item.get("seed", 0))),
    )[0]
    preserved_refold = candidate_artifacts / primary["pdb"]
    observed_refold_hash = file_sha256(preserved_refold)
    declared_design_hash = str(row.get("design_pdb_hash") or "").strip().lower()
    if not declared_design_hash or not observed_refold_hash.startswith(declared_design_hash):
        raise ContractError(
            "replay_design_hash_mismatch",
            "preserved monomer is not the Design refold recorded in candidate_index.csv",
        )

    design_dir = output_root / "design" / args.candidate
    design_dir.mkdir(parents=True)
    local_refold = design_dir / "refold.pdb"
    shutil.copy2(preserved_refold, local_refold)
    manifest = {
        "design_pipeline_version": "5.1.0-preserved-replay",
        "candidate_id": args.candidate,
        "sequence": row["sequence"].upper(),
        "length": len(row["sequence"]),
        "source_route": row.get("source_route", ""),
        "source_batch": row.get("source_batch", ""),
        "cyclization_type": row.get("cyclization_type", ""),
        "refold_pdb": str(local_refold),
        "refold_pdb_hash": observed_refold_hash,
        "backbone_pdb": "",
        "replay_note": (
            "Original Design backbone was not preserved locally; L7 must remain pending."
        ),
    }
    manifest_path = design_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    replay_row = dict(row)
    replay_row.update({
        "manifest_path": str(manifest_path),
        "design_pdb_path": str(local_refold),
        "design_pdb_hash": observed_refold_hash,
    })
    project = _approved_project(_target_mapping(args.target))
    source_state = json.loads((design_product / "state.json").read_text(encoding="utf-8"))
    thresholds = source_state.get("thresholds") or {}

    data_layer.DATA_DIR = output_root / "data"
    data_layer.EVIDENCE_DIR = output_root / "evidence"
    data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
    data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
    data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
    source_state["project_config"] = project
    State.save(source_state)
    CandidateIndex.add(replay_row)
    counter_before = State.load().get("candidate_count")

    summary = PredictionPipeline(
        candidate_rows=CandidateIndex.load(),
        project=project,
        thresholds=thresholds,
        artifacts_root=artifact_root,
        run_root=output_root / "runs",
        config=PredictionConfig(),
        candidate_ids=[args.candidate],
        run_id=args.run_id,
    ).run()
    counter_after = State.load().get("candidate_count")
    if counter_after != counter_before:
        raise ContractError(
            "replay_candidate_counter_changed",
            f"Prediction changed candidate_count {counter_before} -> {counter_after}",
        )

    record_path = output_root / "runs" / args.run_id / "records" / f"{args.candidate}.json"
    replay_manifest = {
        "schema_version": 1,
        "candidate_id": args.candidate,
        "source_design_product": str(design_product),
        "source_artifacts": str(source_root),
        "source_artifacts_sha256": file_sha256(source_bundle_path),
        "design_refold_sha256": observed_refold_hash,
        "original_design_hash_prefix": declared_design_hash,
        "original_design_backbone_preserved": False,
        "candidate_count_before": counter_before,
        "candidate_count_after": counter_after,
        "record": str(record_path),
        "record_sha256": file_sha256(record_path),
        "summary": summary,
    }
    replay_manifest_path = output_root / "replay_manifest.json"
    replay_manifest_path.write_text(
        json.dumps(replay_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return replay_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design-product", required=True)
    parser.add_argument("--source-candidate-artifacts", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "replay_failed"
        print(json.dumps({"status": "error", "code": code, "message": str(exc)}))
        return 2
    print(json.dumps({"status": "complete", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
