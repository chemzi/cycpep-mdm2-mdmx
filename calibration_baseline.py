"""Versioned authority contract for formally published threshold calibration.

This module binds existing calibration output to project, protocol, dataset,
threshold, and artifact identities.  It does not select or change thresholds.
"""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.integrity import file_sha256, object_sha256
from threshold_calibration import (
    ControlDataError,
    validate_control_metadata,
    validate_control_provenance,
)


CALIBRATION_BASELINE_SCHEMA_VERSION = 1
THRESHOLD_SNAPSHOT_SCHEMA_VERSION = 1
CALIBRATION_AUTHORITIES = frozenset({"simulation_only", "approved_real"})
SCIENTIFIC_BINDING_KEYS = (
    "schema_version",
    "threshold_schema_version",
    "calibration_authority",
    "project_id",
    "approved_digest",
    "protocol_identity",
    "scoring_implementation",
    "dataset_sha256",
    "thresholds_sha256",
    "calibration_audit_sha256",
    "calibrated_keys",
)


class CalibrationBaselineError(ValueError):
    """A calibration cannot enter or be consumed from formal authority."""


def _dataset_metadata(dataset: dict) -> dict:
    metadata = dataset.get("metadata") if isinstance(dataset, dict) else None
    if not isinstance(metadata, dict):
        raise CalibrationBaselineError("calibration dataset metadata is required")
    return metadata


def _controls(dataset: dict) -> list[dict]:
    controls = dataset.get("controls") if isinstance(dataset, dict) else None
    if not isinstance(controls, list) or not controls:
        raise CalibrationBaselineError("calibration dataset controls are required")
    if not all(isinstance(control, dict) for control in controls):
        raise CalibrationBaselineError("calibration controls must be objects")
    return controls


def _is_synthetic(control: dict) -> bool:
    source = control.get("source") or {}
    role = str(control.get("role") or "").casefold()
    return bool(isinstance(source, dict) and source.get("synthetic") is True) or (
        "synthetic" in role or "simulation" in role
    )


def _validate_authority(dataset: dict, authority: str) -> None:
    if authority not in CALIBRATION_AUTHORITIES:
        raise CalibrationBaselineError(
            f"calibration_authority must be one of {sorted(CALIBRATION_AUTHORITIES)}"
        )
    metadata = _dataset_metadata(dataset)
    declared = metadata.get("calibration_authority")
    controls = _controls(dataset)
    synthetic = [_is_synthetic(control) for control in controls]
    if authority == "simulation_only":
        if declared != "simulation_only" or not all(synthetic):
            raise CalibrationBaselineError(
                "simulation_only datasets require explicit synthetic provenance"
            )
        return
    if declared != "approved_real" or any(synthetic):
        raise CalibrationBaselineError(
            "synthetic or simulation controls cannot use approved_real authority"
        )


def _validate_dataset_binding(
    dataset: dict, project: dict, protocol_identity: dict
) -> None:
    content = deepcopy(project)
    content.pop("review", None)
    approved_digest = (project.get("review") or {}).get("approved_digest")
    if not approved_digest or approved_digest != object_sha256(content):
        raise CalibrationBaselineError("project approved_digest mismatch")
    metadata = _dataset_metadata(dataset)
    if str(metadata.get("schema_version")) != "2":
        raise CalibrationBaselineError(
            "formal calibration publication requires control dataset schema_version 2"
        )
    try:
        validate_control_metadata(
            metadata,
            project_id=project.get("project_id"),
            approved_digest=approved_digest,
            protocol=protocol_identity,
        )
        validate_control_provenance(
            _controls(dataset), version=metadata.get("schema_version")
        )
    except ControlDataError as exc:
        raise CalibrationBaselineError(str(exc)) from exc


def _validate_calibrated_output(thresholds: dict, audit: dict) -> list[str]:
    keys = audit.get("calibrated_keys") if isinstance(audit, dict) else None
    if not isinstance(keys, list) or not keys:
        raise CalibrationBaselineError(
            "a formal calibration baseline requires at least one calibrated metric"
        )
    claimed: set[str] = set()
    for metric_key, entry in thresholds.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("calibration_status") == "calibrated":
            claimed.add(str(metric_key))
        for target_id, target_entry in (entry.get("targets") or {}).items():
            if (
                isinstance(target_entry, dict)
                and target_entry.get("calibration_status") == "calibrated"
            ):
                claimed.add(f"{metric_key}:{target_id}")
    expected = {str(key) for key in keys}
    if claimed != expected:
        raise CalibrationBaselineError(
            "calibrated threshold claims do not match calibration audit"
        )
    for scope_key in keys:
        metric_key, _, target_id = str(scope_key).partition(":")
        entry = thresholds.get(metric_key)
        if target_id and isinstance(entry, dict):
            entry = (entry.get("targets") or {}).get(target_id)
        metric_audit = (audit.get("metrics") or {}).get(scope_key)
        if (
            not isinstance(entry, dict)
            or entry.get("calibration_status") != "calibrated"
            or not isinstance(metric_audit, dict)
            or metric_audit.get("status") != "calibrated"
            or entry.get("value") != metric_audit.get("value")
        ):
            raise CalibrationBaselineError(
                f"calibrated output mismatch for {scope_key}"
            )
    return sorted(str(key) for key in keys)


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_calibration_publication(
    *,
    dataset: dict,
    thresholds: dict,
    audit: dict,
    project: dict,
    calibration_authority: str,
    protocol_identity: dict,
    scoring_implementation: dict,
    artifact_path: str | Path,
) -> dict:
    """Create a deterministic artifact and its formal publication binding."""
    _validate_authority(dataset, calibration_authority)
    if not isinstance(protocol_identity, dict) or not protocol_identity:
        raise CalibrationBaselineError("protocol_identity is required")
    if not isinstance(scoring_implementation, dict) or not scoring_implementation:
        raise CalibrationBaselineError("scoring_implementation is required")
    _validate_dataset_binding(dataset, project, protocol_identity)
    calibrated_keys = _validate_calibrated_output(thresholds, audit)

    scientific_binding = {
        "schema_version": CALIBRATION_BASELINE_SCHEMA_VERSION,
        "threshold_schema_version": THRESHOLD_SNAPSHOT_SCHEMA_VERSION,
        "calibration_authority": calibration_authority,
        "project_id": project.get("project_id"),
        "approved_digest": (project.get("review") or {}).get("approved_digest"),
        "protocol_identity": dict(protocol_identity),
        "scoring_implementation": dict(scoring_implementation),
        "dataset_sha256": object_sha256(dataset),
        "thresholds_sha256": object_sha256(thresholds),
        "calibration_audit_sha256": object_sha256(audit),
        "calibrated_keys": calibrated_keys,
    }
    scientific_sha256 = object_sha256(scientific_binding)
    publication_id = f"calibration-{scientific_sha256}"
    artifact_id = f"{publication_id}-artifact"
    artifact = {
        "schema_version": CALIBRATION_BASELINE_SCHEMA_VERSION,
        "artifact_type": "calibration_baseline",
        "scientific_binding": scientific_binding,
        "dataset": dataset,
        "calibration_audit": audit,
    }
    path = Path(artifact_path).expanduser().resolve()
    _atomic_write_json(path, artifact)
    artifact_sha256 = file_sha256(path)
    binding = {
        **scientific_binding,
        "scientific_binding_sha256": scientific_sha256,
        "publication_id": publication_id,
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
    }
    return {
        "binding": binding,
        "thresholds": thresholds,
        "artifact": {
            "artifact_id": artifact_id,
            "artifact_type": "calibration_baseline",
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": artifact_sha256,
        },
    }


def validate_publication_identity(binding: dict) -> None:
    """Reject a caller-supplied publication id that is not its natural id."""
    scientific = {
        key: binding.get(key)
        for key in SCIENTIFIC_BINDING_KEYS
    }
    digest = object_sha256(scientific)
    if binding.get("scientific_binding_sha256") != digest:
        raise CalibrationBaselineError("scientific binding digest mismatch")
    if binding.get("publication_id") != f"calibration-{digest}":
        raise CalibrationBaselineError("publication_id does not match natural identity")
    if binding.get("artifact_id") != f"calibration-{digest}-artifact":
        raise CalibrationBaselineError("artifact_id does not match publication identity")


def validate_calibration_consumption(
    *,
    binding: dict,
    thresholds: dict,
    project: dict,
    artifact: dict | None,
    protocol_identity: dict,
    scoring_implementation: dict,
) -> dict:
    """Validate and return the exact formal binding consumed by Prediction."""
    if not isinstance(binding, dict):
        raise CalibrationBaselineError("formal calibration binding is required")
    validate_publication_identity(binding)
    if binding.get("calibration_authority") not in CALIBRATION_AUTHORITIES:
        raise CalibrationBaselineError("calibration authority is invalid")
    content = deepcopy(project)
    content.pop("review", None)
    approved_digest = (project.get("review") or {}).get("approved_digest")
    if not approved_digest or object_sha256(content) != approved_digest:
        raise CalibrationBaselineError("active project approved_digest mismatch")
    if (
        binding.get("project_id") != project.get("project_id")
        or binding.get("approved_digest") != approved_digest
    ):
        raise CalibrationBaselineError("calibration binding project mismatch")
    if binding.get("protocol_identity") != protocol_identity:
        raise CalibrationBaselineError("calibration binding protocol mismatch")
    if binding.get("scoring_implementation") != scoring_implementation:
        raise CalibrationBaselineError("calibration binding scoring implementation mismatch")
    if object_sha256(thresholds) != binding.get("thresholds_sha256"):
        raise CalibrationBaselineError("calibration threshold snapshot mismatch")
    if not isinstance(artifact, dict) or artifact.get("artifact_id") != binding.get(
        "artifact_id"
    ):
        raise CalibrationBaselineError("calibration artifact is not registered")
    if artifact.get("sha256") != binding.get("artifact_sha256"):
        raise CalibrationBaselineError("calibration artifact registry digest mismatch")
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file() or file_sha256(path) != binding.get("artifact_sha256"):
        raise CalibrationBaselineError("calibration artifact content mismatch")
    validate_calibration_artifact(binding, path, thresholds=thresholds)
    return dict(binding)


def validate_calibration_artifact(
    binding: dict,
    path: str | Path,
    *,
    thresholds: dict | None = None,
) -> dict:
    """Validate the scientific binding and provenance embedded in an artifact."""
    try:
        artifact_content = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationBaselineError("calibration artifact content is invalid") from exc
    expected_scientific = {key: binding.get(key) for key in SCIENTIFIC_BINDING_KEYS}
    if artifact_content.get("scientific_binding") != expected_scientific:
        raise CalibrationBaselineError("calibration artifact scientific binding mismatch")
    dataset = artifact_content.get("dataset")
    if not isinstance(dataset, dict) or object_sha256(dataset) != binding.get(
        "dataset_sha256"
    ):
        raise CalibrationBaselineError("calibration artifact dataset mismatch")
    if str(_dataset_metadata(dataset).get("schema_version")) != "2":
        raise CalibrationBaselineError(
            "formal calibration publication requires control dataset schema_version 2"
        )
    _validate_authority(dataset, str(binding.get("calibration_authority") or ""))
    try:
        validate_control_metadata(
            _dataset_metadata(dataset),
            project_id=str(binding.get("project_id") or ""),
            approved_digest=str(binding.get("approved_digest") or ""),
            protocol=binding.get("protocol_identity"),
        )
        validate_control_provenance(_controls(dataset), version=2)
    except ControlDataError as exc:
        raise CalibrationBaselineError(str(exc)) from exc
    audit = artifact_content.get("calibration_audit")
    if not isinstance(audit, dict) or object_sha256(audit) != binding.get(
        "calibration_audit_sha256"
    ):
        raise CalibrationBaselineError("calibration artifact audit mismatch")
    if thresholds is not None:
        calibrated_keys = _validate_calibrated_output(thresholds, audit)
        if calibrated_keys != binding.get("calibrated_keys"):
            raise CalibrationBaselineError(
                "calibration binding calibrated keys do not match artifact audit"
            )
    return artifact_content


def thresholds_claim_formal_calibration(thresholds: dict) -> bool:
    """Return whether any base or target override claims calibrated status."""
    for entry in thresholds.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("calibration_status") == "calibrated":
            return True
        targets = entry.get("targets")
        if isinstance(targets, dict) and any(
            isinstance(value, dict)
            and value.get("calibration_status") == "calibrated"
            for value in targets.values()
        ):
            return True
    return False


def unpublished_calibration_binding(thresholds: dict) -> dict:
    return {
        "status": "not_published",
        "calibration_authority": None,
        "thresholds_sha256": object_sha256(thresholds),
    }
