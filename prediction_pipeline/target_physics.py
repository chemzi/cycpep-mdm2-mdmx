"""Aggregate target-level PRODIGY and Rosetta evidence."""

from __future__ import annotations

import statistics

from .adapters import parse_metadata
from .contracts import ContractError
from .metrics import parse_prodigy_output, parse_rosetta_interface_output
from .model_rejections import model_identity


def _prodigy_samples(entries: list[dict]) -> list[dict]:
    samples = []
    for entry in entries:
        parsed = parse_prodigy_output(
            entry["output"]["path"].read_text(encoding="utf-8", errors="replace")
        )
        samples.append({
            "predictor": entry["predictor"], "model_id": entry["model_id"],
            "seed": entry["seed"],
            "prediction_pdb_sha256": entry["prediction_pdb_sha256"],
            "artifact": str(entry["output"]["path"]),
            "sha256": entry["output"]["sha256"], "metrics": parsed,
        })
    return samples


def _rosetta_samples(entries: list[dict]) -> list[dict]:
    samples = []
    for entry in entries:
        parsed = parse_rosetta_interface_output(
            entry["output"]["path"].read_text(encoding="utf-8", errors="replace")
        )
        samples.append({
            "predictor": entry["predictor"], "model_id": entry["model_id"],
            "seed": entry["seed"],
            "prediction_pdb_sha256": entry["prediction_pdb_sha256"],
            "artifact": str(entry["output"]["path"]),
            "sha256": entry["output"]["sha256"],
            "metadata_artifact": (
                str(entry["metadata"]["path"]) if entry.get("metadata") else None
            ),
            "metadata_sha256": (
                entry["metadata"]["sha256"] if entry.get("metadata") else None
            ),
            "metrics": parsed,
        })
    return samples


def parse_target_physics(target_artifacts: dict) -> tuple[dict, list[dict]]:
    metrics, provenance = {}, []
    rosetta_outputs = target_artifacts.get("rosetta_outputs") or []
    rejections = target_artifacts.get("rosetta_rejections") or []
    legacy_rosetta = target_artifacts.get("rosetta_output")
    if rejections:
        metrics["rosetta_scientific_rejections"] = rejections
        provenance.append({
            "evidence": "rosetta_scientific_rejections",
            "aggregation": "typed_model_rejections", "rejections": rejections,
            "metrics": ["rosetta_scientific_rejections"],
        })

    prodigy_entries = target_artifacts.get("prodigy_outputs") or []
    legacy_prodigy = target_artifacts.get("prodigy_output")
    if prodigy_entries:
        declared = _prodigy_samples(prodigy_entries)
        methods = {sample["metrics"].get("dg_method") for sample in declared}
        if len(methods) != 1:
            raise ContractError(
                "prodigy_method_inconsistent",
                f"PRODIGY outputs use inconsistent methods: {sorted(methods)}",
            )
        eligible = {model_identity(entry) for entry in rosetta_outputs}
        samples = (
            [sample for sample in declared if model_identity(sample) in eligible]
            if rejections else declared
        )
        diagnostic = (
            [sample for sample in declared if model_identity(sample) not in eligible]
            if rejections else []
        )
        if samples:
            metrics["dg"] = float(statistics.median(
                sample["metrics"]["dg"] for sample in samples
            ))
            metrics["dg_method"] = methods.pop()
        provenance.append({
            "tool": "PRODIGY",
            "aggregation": "median_across_rosetta_eligible_predictions"
            if rejections else "median_across_declared_predictions",
            "samples": samples, "diagnostic_samples": diagnostic,
            "metrics": [key for key in ("dg", "dg_method") if key in metrics],
        })
    elif legacy_prodigy:
        parsed = parse_prodigy_output(
            legacy_prodigy["path"].read_text(encoding="utf-8", errors="replace")
        )
        metrics.update(parsed)
        provenance.append({
            "tool": "PRODIGY", "aggregation": "legacy_single_prediction",
            "artifact": str(legacy_prodigy["path"]),
            "sha256": legacy_prodigy["sha256"], "metrics": sorted(parsed),
        })

    if rosetta_outputs:
        samples = _rosetta_samples(rosetta_outputs)
        metrics["sc"] = float(statistics.median(
            sample["metrics"]["sc"] for sample in samples
        ))
        metrics["dsasa"] = float(statistics.median(
            sample["metrics"]["dsasa"] for sample in samples
        ))
        rosetta_dg = [
            sample["metrics"].get("rosetta_dg_separated") for sample in samples
        ]
        if all(value is not None for value in rosetta_dg):
            metrics["rosetta_dg_separated"] = float(statistics.median(rosetta_dg))
        provenance.append({
            "tool": "Rosetta InterfaceAnalyzer",
            "aggregation": "median_across_rosetta_eligible_predictions"
            if rejections else "median_across_declared_predictions",
            "samples": samples,
            "metrics": [key for key in ("sc", "dsasa", "rosetta_dg_separated")
                        if key in metrics],
        })
    elif legacy_rosetta:
        parsed = parse_rosetta_interface_output(
            legacy_rosetta["path"].read_text(encoding="utf-8", errors="replace")
        )
        metrics.update(parsed)
        provenance.append({
            "tool": "Rosetta InterfaceAnalyzer",
            "artifact": str(legacy_rosetta["path"]),
            "sha256": legacy_rosetta["sha256"], "metrics": sorted(parsed),
        })
    return metrics, provenance
