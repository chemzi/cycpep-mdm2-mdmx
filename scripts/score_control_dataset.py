"""Score a KEAP1 control manifest and emit a bound v2 control dataset.

The prediction pipeline that computes the battery metrics for a control
requires GPU-backed scientific tools and runs on the server.  This script is
the integration point: it loads the control manifest (provenance-only), merges
per-control metric values produced by that pipeline, and emits a bound schema
v2 control dataset that ``threshold_calibration.load_control_dataset`` accepts.

Scores file format (JSON)::

    {
      "keap1-7K2E-positive": {
        "global": {"plddt": 0.9, "nc_distance_pre": 1.2, "nc_distance_post": 1.3, "scrmsd": 0.8},
        "targets": {"KEAP1": {"ipsae": 0.8, "hotspot_cov": 0.9, "pose_rmsd": 1.0, ...}}
      },
      ...
    }

The metric names are the same ones ``threshold_calibration._control_value``
reads, so a candidate row from the prediction pipeline can be passed through
unchanged.  The binding metadata (project id, approved digest, protocol) is
filled from the approved project config at run time; nothing is baked into the
shipped manifest.

Use ``--dry-run`` to validate the manifest without scores (CI/local check).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from project_config import load_project_config, required_target_ids


def _control_metrics(control: dict, scores: dict) -> dict:
    """Extract the metric payload for one control from the scores map."""
    control_id = control["control_id"]
    if control_id not in scores:
        raise ValueError(f"no scores for control {control_id!r}")
    metrics = scores[control_id]
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError(f"scores for control {control_id!r} are empty")
    return metrics


def build_scored_dataset(
    manifest_path: Path,
    scores: dict,
    *,
    config: dict,
    target_ids: list[str],
) -> dict:
    """Assemble a bound v2 control dataset from a manifest and scores."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(payload.get("schema_version")) != "2":
        raise ValueError("control manifest must use schema_version 2")
    controls = [item for item in payload.get("controls", []) if isinstance(item, dict)]
    if not controls:
        raise ValueError("control manifest contains no controls")
    selection = config.get("selection") or {}
    protocol = selection.get("calibration_protocol") or config.get("calibration_protocol")
    records = []
    for control in controls:
        record = {
            "control_id": control["control_id"],
            "label": control["label"],
            "role": control.get("role"),
            "sequence": control.get("sequence"),
            "source": control.get("source"),
            "metrics": _control_metrics(control, scores),
        }
        records.append({key: value for key, value in record.items() if value is not None})
    metadata = {
        "project_id": config.get("project_id"),
        "approved_digest": (config.get("review") or {}).get("approved_digest"),
        "schema_version": 2,
        "protocol": protocol,
        "source_manifest": str(manifest_path),
        "target_ids": target_ids,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"metadata": metadata, "controls": records}


def main() -> int:
    parser = argparse.ArgumentParser(description="score a KEAP1 control manifest")
    parser.add_argument("--manifest", required=True, help="control manifest (schema v2)")
    parser.add_argument("--scores", default=None, help="JSON map of control_id -> metrics")
    parser.add_argument("--output", required=True, help="bound v2 control dataset output path")
    parser.add_argument("--dry-run", action="store_true", help="validate manifest only")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    if args.dry_run:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        counts = {
            "positive": sum(1 for c in payload.get("controls", []) if c.get("label") == "positive"),
            "negative": sum(1 for c in payload.get("controls", []) if c.get("label") == "negative"),
        }
        print(json.dumps({"status": "dry_run_ok", "counts": counts}, ensure_ascii=False))
        return 0
    if not args.scores:
        print("--scores is required unless --dry-run", file=sys.stderr)
        return 2
    scores_path = Path(args.scores)
    if not scores_path.is_file():
        print(f"scores not found: {scores_path}", file=sys.stderr)
        return 2

    config = load_project_config()
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    dataset = build_scored_dataset(
        manifest_path,
        scores,
        config=config,
        target_ids=list(required_target_ids(config)),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "n_controls": len(dataset["controls"]),
        "project_id": dataset["metadata"]["project_id"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
