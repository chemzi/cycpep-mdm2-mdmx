"""Calibrate Research thresholds from labelled positive/negative controls.

Examples::

    python -m scripts.calibrate_thresholds --controls data/controls.json
    cat data/controls.json | python -m scripts.calibrate_thresholds

The command is offline and never mutates ``state.json``.  Research invokes the
same library through ``agents.research`` and persists the resulting audit in
``_threshold_calibration.json`` and the threshold cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agents.research import default_thresholds
from project_config import load_project_config, required_target_ids
from threshold_calibration import (
    ControlDataError,
    CALIBRATION_SCHEMA_VERSION,
    calibrate_thresholds,
    load_control_dataset,
    validate_control_metadata,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="positive/negative control threshold calibration")
    parser.add_argument("--controls", default=None, help="JSON control dataset path")
    parser.add_argument("--max-fpr", type=float, default=0.05)
    parser.add_argument("--min-positive-recall", type=float, default=0.50)
    parser.add_argument("--min-negative", type=int, default=10)
    parser.add_argument("--min-positive", type=int, default=3)
    args = parser.parse_args()

    config = load_project_config()
    selection = config.get("selection") or {}
    expected_protocol = selection.get("calibration_protocol") or config.get(
        "calibration_protocol"
    )
    expected_protocol_hash = selection.get("calibration_protocol_hash") or config.get(
        "calibration_protocol_hash"
    )
    path = args.controls or os.environ.get("CYCPEP_CONTROL_DATA")
    try:
        if path:
            controls, metadata = load_control_dataset(
                path,
                project_id=config.get("project_id"),
                approved_digest=(config.get("review") or {}).get("approved_digest"),
                protocol=expected_protocol,
                protocol_hash=expected_protocol_hash,
                schema_version=CALIBRATION_SCHEMA_VERSION,
            )
        else:
            raw = json.load(sys.stdin)
            from threshold_calibration import coerce_dataset

            controls, metadata = coerce_dataset(raw)
            metadata = validate_control_metadata(
                metadata,
                project_id=config.get("project_id"),
                approved_digest=(config.get("review") or {}).get("approved_digest"),
                protocol=expected_protocol,
                protocol_hash=expected_protocol_hash,
                schema_version=CALIBRATION_SCHEMA_VERSION,
            )
        protocol = expected_protocol or metadata.get("protocol")
        protocol_hash = metadata.get("protocol_hash") or expected_protocol_hash
        thresholds, audit = calibrate_thresholds(
            controls=controls,
            thresholds=default_thresholds(config),
            target_ids=required_target_ids(config),
            protocol=protocol,
            protocol_hash=protocol_hash,
            max_false_positive_rate=args.max_fpr,
            min_positive_recall=args.min_positive_recall,
            min_negative_controls=args.min_negative,
            min_positive_controls=args.min_positive,
        )
        print(json.dumps({"thresholds": thresholds, "_meta": audit}, ensure_ascii=False, indent=2))
        return 0
    except (ControlDataError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"thresholds": default_thresholds(config), "_meta": {
                    "status": "invalidated", "error": f"{type(exc).__name__}: {exc}"
                }},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
