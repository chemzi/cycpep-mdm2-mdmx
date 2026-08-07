"""Explicitly bind the current prediction protocol to legacy artifact bundles.

Runtime code (enrichment, resume) refuses to guess a protocol for evidence
that predates protocol recording.  This script is the deliberate, auditable
path for an operator to bind legacy ``artifacts.json`` files -- and the
prediction output directories they reference -- to the current protocol.

Only bundles without a ``protocol`` binding are touched.  A bundle that
already records a different protocol is reported as an error and left
untouched, because silently rewriting history is exactly what this migration
exists to avoid.

Usage:
    python scripts/migrate_legacy_prediction_protocol.py PATH [PATH ...] [--dry-run]

``PATH`` may be an ``artifacts.json`` file or a directory tree searched
recursively for ``artifacts.json`` files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.supervisor import atomic_json  # noqa: E402
from prediction_pipeline.protocol import (  # noqa: E402
    PREDICTION_PROTOCOL,
    PREDICTOR_PROTOCOL,
    protocol_binding,
)


PROTOCOL_BINDING_FILENAME = "protocol_binding.json"


def _iter_predictions(bundle: dict):
    """Yield the prediction entries referenced by a bundle."""
    global_raw = bundle.get("global")
    if isinstance(global_raw, dict):
        for prediction in global_raw.get("monomer_predictions") or []:
            if isinstance(prediction, dict):
                yield prediction
    targets = bundle.get("targets")
    if isinstance(targets, dict):
        for target in targets.values():
            if not isinstance(target, dict):
                continue
            for prediction in target.get("complex_predictions") or []:
                if isinstance(prediction, dict):
                    yield prediction


def _prediction_output_dirs(bundle: dict, base: Path) -> set[Path]:
    """Collect the output directories referenced by a bundle's predictions."""
    dirs: set[Path] = set()
    for prediction in _iter_predictions(bundle):
        metadata = prediction.get("metadata")
        if isinstance(metadata, str) and metadata:
            dirs.add(base / Path(metadata).parent)
    return dirs


def _parameter_differences(bundle: dict, base: Path) -> list[str]:
    """Report ColabDesign metadata parameters that differ from the protocol.

    Binding a legacy bundle records the current protocol SHA, but migration
    itself does not change the evidence; warn when the recorded parameters
    provably do not follow the current protocol so the operator can decide
    whether regeneration is required.
    """
    af2 = PREDICTION_PROTOCOL["af2_prodigy"]
    differences: list[str] = []
    for prediction in _iter_predictions(bundle):
        if prediction.get("predictor") != "ColabDesign":
            continue
        raw_metadata = prediction.get("metadata")
        if not isinstance(raw_metadata, str) or not raw_metadata:
            continue
        metadata_path = base / raw_metadata
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(metadata, dict):
            continue
        num_recycles = metadata.get("num_recycles")
        if isinstance(num_recycles, int) and num_recycles != af2["num_recycles"]:
            differences.append(
                f"{metadata_path}: num_recycles={num_recycles} != "
                f"protocol {af2['num_recycles']}"
            )
        model_number = metadata.get("model_number")
        if (
            isinstance(model_number, int)
            and model_number not in af2["model_numbers"]
        ):
            differences.append(
                f"{metadata_path}: model_number={model_number} not in "
                f"protocol model_numbers {af2['model_numbers']}"
            )
        seed = metadata.get("seed")
        if isinstance(seed, int) and seed not in af2["seeds"]:
            differences.append(
                f"{metadata_path}: seed={seed} not in protocol seeds {af2['seeds']}"
            )
    return differences


def _warning_suffix(differences: list[str]) -> str:
    if not differences:
        return ""
    return (
        "; warning: recorded parameters differ from the current protocol: "
        + "; ".join(differences)
    )


def _dir_binding_matches(output_dir: Path) -> bool:
    """Return whether the output dir already carries the current binding."""
    binding_path = output_dir / PROTOCOL_BINDING_FILENAME
    if not binding_path.is_file():
        return False
    try:
        recorded = json.loads(binding_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False
    return recorded == protocol_binding()


def migrate_bundle(bundle_path: Path, *, dry_run: bool = False) -> str:
    """Bind one legacy bundle to the current protocol; return a status line."""
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return f"error: {bundle_path}: cannot read bundle ({exc})"
    if not isinstance(raw, dict):
        return f"error: {bundle_path}: artifacts.json must be an object"
    if not isinstance(raw.get("candidate_id"), str) or not raw.get("schema_version"):
        return (
            f"error: {bundle_path}: not an artifact bundle "
            "(missing candidate_id/schema_version)"
        )
    existing = raw.get("protocol")
    output_dirs = _prediction_output_dirs(raw, bundle_path.parent)
    differences = _parameter_differences(raw, bundle_path.parent)
    warning = _warning_suffix(differences)
    if existing == protocol_binding():
        reparable = [
            output_dir for output_dir in output_dirs
            if output_dir.is_dir() and not _dir_binding_matches(output_dir)
        ]
        if not reparable:
            return f"skip: {bundle_path}: already bound to the current protocol"
        if dry_run:
            return (
                f"would-repair: {bundle_path} "
                f"({len(reparable)} output dirs missing bindings){warning}"
            )
        for output_dir in sorted(reparable):
            atomic_json(output_dir / PROTOCOL_BINDING_FILENAME, protocol_binding())
        return (
            f"repaired: {bundle_path} "
            f"(bound {PREDICTOR_PROTOCOL} to {len(reparable)} output dirs){warning}"
        )
    if existing is not None:
        return (
            f"error: {bundle_path}: recorded protocol differs from the current "
            "protocol; refusing to relabel historical evidence"
        )
    if dry_run:
        return (
            f"would-migrate: {bundle_path} "
            f"(bind {PREDICTOR_PROTOCOL} to {len(output_dirs)} output dirs){warning}"
        )
    raw["protocol"] = protocol_binding()
    atomic_json(bundle_path, raw)
    bound_dirs = 0
    missing_dirs = 0
    for output_dir in sorted(output_dirs):
        if not output_dir.is_dir():
            missing_dirs += 1
            continue
        atomic_json(output_dir / PROTOCOL_BINDING_FILENAME, protocol_binding())
        bound_dirs += 1
    missing_note = f", {missing_dirs} missing" if missing_dirs else ""
    return (
        f"migrated: {bundle_path} "
        f"(bound {PREDICTOR_PROTOCOL}, {bound_dirs} output dirs{missing_note}){warning}"
    )


def _resolve_targets(raw_paths: list[str]) -> list[Path]:
    targets: list[Path] = []
    for raw in raw_paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            targets.extend(sorted(path.rglob("artifacts.json")))
        elif path.is_file():
            if path.name != "artifacts.json":
                raise SystemExit(
                    f"error: not an artifacts.json bundle file: {path}"
                )
            targets.append(path)
        else:
            raise SystemExit(f"error: not found: {path}")
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+",
        help="artifacts.json file(s) or directories",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()
    statuses = [
        migrate_bundle(path, dry_run=args.dry_run)
        for path in _resolve_targets(args.paths)
    ]
    for status in statuses:
        print(status)
    errors = [status for status in statuses if status.startswith("error:")]
    migrated = [status for status in statuses if status.startswith("migrated:")]
    print(
        f"summary: {len(migrated)} migrated, {len(errors)} errors, "
        f"{len(statuses) - len(migrated) - len(errors)} skipped"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
