"""Versioned authority contract for formally published threshold calibration.

This module binds existing calibration output to project, protocol, dataset,
threshold, and artifact identities.  It does not select or change thresholds.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.integrity import file_sha256, object_sha256
from threshold_calibration import (
    ControlDataError,
    validate_control_metadata,
    validate_control_provenance,
)
from project_config import required_target_ids


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
    "approved_scored_dataset_sha256",
    "calibration_parameters_sha256",
    "thresholds_sha256",
    "calibration_audit_sha256",
    "calibrated_keys",
)


class CalibrationBaselineError(ValueError):
    """A calibration cannot enter or be consumed from formal authority."""


class _ValidatedCalibrationBinding(Mapping[str, Any]):
    """Internal proof that the formal Store consumption seam validated a binding."""

    def __init__(self, binding: dict):
        self.__binding = deepcopy(binding)

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self.__binding[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self.__binding)

    def __len__(self) -> int:
        return len(self.__binding)


def is_validated_calibration_binding(value: object) -> bool:
    return isinstance(value, _ValidatedCalibrationBinding)


def validated_calibration_binding_for_runtime(
    value: object, *, thresholds: dict, project: dict
) -> dict:
    if not is_validated_calibration_binding(value):
        raise CalibrationBaselineError(
            "formal calibration claims require a Store-validated binding"
        )
    binding = dict(value)
    validate_publication_identity(binding)
    _validate_prediction_owner(
        binding.get("protocol_identity"), binding.get("scoring_implementation")
    )
    validate_approved_project(project)
    if (
        binding.get("calibration_authority") != "simulation_only"
        or binding.get("project_id") != project.get("project_id")
        or binding.get("approved_digest")
        != (project.get("review") or {}).get("approved_digest")
        or binding.get("thresholds_sha256") != object_sha256(thresholds)
    ):
        raise CalibrationBaselineError(
            "validated calibration binding does not match Pipeline runtime"
        )
    return binding


def validate_approved_project(project: dict) -> None:
    """Apply the repository's canonical project-approval semantics."""
    from target_bootstrap import ReviewRequiredError, assert_project_approved

    try:
        assert_project_approved(project)
    except ReviewRequiredError as exc:
        raise CalibrationBaselineError(str(exc)) from exc


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


def _validate_authority(
    dataset: dict,
    authority: str,
) -> None:
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
    raise CalibrationBaselineError("approved_real publication is unavailable in E1")


def _prediction_owner_identities() -> tuple[dict, dict]:
    from prediction_pipeline.contracts import scoring_implementation_identity
    from prediction_pipeline.protocol import protocol_binding

    return protocol_binding(), scoring_implementation_identity()


def _validate_prediction_owner(
    protocol_identity: dict, scoring_implementation: dict
) -> None:
    owner_protocol, owner_scoring = _prediction_owner_identities()
    if (
        protocol_identity != owner_protocol
        or scoring_implementation != owner_scoring
    ):
        raise CalibrationBaselineError(
            "calibration publication must use Prediction-owned protocol and scoring identity"
        )


def _validate_dataset_binding(
    dataset: dict,
    project: dict,
    protocol_identity: dict,
    scoring_implementation: dict,
) -> dict:
    validate_approved_project(project)
    approved_digest = (project.get("review") or {}).get("approved_digest")
    metadata = _dataset_metadata(dataset)
    if str(metadata.get("schema_version")) != "2":
        raise CalibrationBaselineError(
            "formal calibration publication requires control dataset schema_version 2"
        )
    try:
        normalized = validate_control_metadata(
            metadata,
            project_id=project.get("project_id"),
            approved_digest=approved_digest,
            protocol=protocol_identity,
            scoring_implementation=scoring_implementation,
        )
        validate_control_provenance(
            _controls(dataset), version=metadata.get("schema_version")
        )
    except ControlDataError as exc:
        raise CalibrationBaselineError(str(exc)) from exc
    return normalized


def _validate_calibrator_lineage(
    *,
    dataset: dict,
    audit: dict,
    protocol_identity: dict,
    scoring_implementation: dict,
    project: dict | None = None,
) -> str:
    dataset_sha256 = object_sha256(dataset)
    if audit.get("control_dataset_sha256") != dataset_sha256:
        raise CalibrationBaselineError(
            "control dataset does not match calibration audit input identity"
        )
    parameters = audit.get("calibration_parameters")
    if (
        not isinstance(parameters, dict)
        or audit.get("calibration_parameters_sha256") != object_sha256(parameters)
    ):
        raise CalibrationBaselineError("calibration parameters identity mismatch")
    required_parameter_keys = {
        "metric_keys",
        "target_ids",
        "max_false_positive_rate",
        "min_positive_recall",
        "min_negative_controls",
        "min_positive_controls",
    }
    if set(parameters) != required_parameter_keys:
        raise CalibrationBaselineError("calibration parameters are incomplete")
    metric_keys = parameters.get("metric_keys")
    target_ids = parameters.get("target_ids")
    if (
        not isinstance(metric_keys, list)
        or not metric_keys
        or metric_keys != sorted(set(metric_keys))
        or not isinstance(target_ids, list)
        or target_ids != sorted(set(target_ids))
    ):
        raise CalibrationBaselineError("calibration metric or target identity is invalid")
    expected_fields = {
        "max_false_positive_rate": audit.get("max_false_positive_rate"),
        "min_positive_recall": audit.get("min_positive_recall"),
        "min_negative_controls": audit.get("min_negative_controls"),
        "min_positive_controls": audit.get("min_positive_controls"),
    }
    if any(parameters.get(key) != value for key, value in expected_fields.items()):
        raise CalibrationBaselineError("calibration parameters do not match audit")
    calibrated_keys = audit.get("calibrated_keys") or []
    if any(
        str(scope_key).partition(":")[0] not in metric_keys
        or (
            str(scope_key).partition(":")[2]
            and str(scope_key).partition(":")[2] not in target_ids
        )
        for scope_key in calibrated_keys
    ):
        raise CalibrationBaselineError(
            "calibrated output does not match calibration parameters"
        )
    if audit.get("protocol_identity") != protocol_identity:
        raise CalibrationBaselineError("calibration audit protocol identity mismatch")
    metadata = _dataset_metadata(dataset)
    if metadata.get("scoring_implementation") != scoring_implementation:
        raise CalibrationBaselineError("control dataset scoring implementation mismatch")
    if audit.get("scoring_implementation") != metadata.get("scoring_implementation"):
        raise CalibrationBaselineError("calibration audit scoring implementation mismatch")
    if project is not None and not set(target_ids).issubset(required_target_ids(project)):
        raise CalibrationBaselineError("calibration target scope is outside approved project")
    try:
        normalized = validate_control_metadata(
            metadata,
            project_id=(project or {}).get("project_id") or metadata.get("project_id"),
            approved_digest=(
                ((project or {}).get("review") or {}).get("approved_digest")
                or metadata.get("approved_digest")
            ),
            protocol=protocol_identity,
            scoring_implementation=scoring_implementation,
        )
    except ControlDataError as exc:
        raise CalibrationBaselineError(str(exc)) from exc
    if audit.get("protocol_hash") != normalized.get("protocol_hash"):
        raise CalibrationBaselineError("calibration audit protocol hash mismatch")
    return str(audit["calibration_parameters_sha256"])


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
        shared_scientific_fields = (
            "value",
            "operator",
            "direction",
            "n_positive",
            "n_negative",
            "protocol_hash",
            "observed_false_positive_rate",
            "positive_recall",
            "false_positive_rate_ci95",
            "positive_recall_ci95",
        )
        if (
            not isinstance(entry, dict)
            or entry.get("calibration_status") != "calibrated"
            or not isinstance(metric_audit, dict)
            or metric_audit.get("status") != "calibrated"
            or any(
                entry.get(field) != metric_audit.get(field)
                for field in shared_scientific_fields
            )
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
    _validate_prediction_owner(protocol_identity, scoring_implementation)
    approved_dataset_sha256 = None
    _validate_authority(dataset, calibration_authority)
    if not isinstance(protocol_identity, dict) or not protocol_identity:
        raise CalibrationBaselineError("protocol_identity is required")
    if not isinstance(scoring_implementation, dict) or not scoring_implementation:
        raise CalibrationBaselineError("scoring_implementation is required")
    _validate_dataset_binding(
        dataset, project, protocol_identity, scoring_implementation
    )
    calibration_parameters_sha256 = _validate_calibrator_lineage(
        dataset=dataset,
        audit=audit,
        protocol_identity=protocol_identity,
        scoring_implementation=scoring_implementation,
        project=project,
    )
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
        "approved_scored_dataset_sha256": approved_dataset_sha256,
        "calibration_parameters_sha256": calibration_parameters_sha256,
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
    _validate_prediction_owner(
        binding.get("protocol_identity"), binding.get("scoring_implementation")
    )
    if binding.get("calibration_authority") not in CALIBRATION_AUTHORITIES:
        raise CalibrationBaselineError("calibration authority is invalid")
    validate_approved_project(project)
    approved_digest = (project.get("review") or {}).get("approved_digest")
    if (
        binding.get("project_id") != project.get("project_id")
        or binding.get("approved_digest") != approved_digest
    ):
        raise CalibrationBaselineError("calibration binding project mismatch")
    if binding.get("protocol_identity") != protocol_identity:
        raise CalibrationBaselineError("calibration binding protocol mismatch")
    if binding.get("scoring_implementation") != scoring_implementation:
        raise CalibrationBaselineError("calibration binding scoring implementation mismatch")
    if binding.get("calibration_authority") != "simulation_only":
        raise CalibrationBaselineError("approved_real consumption is unavailable in E1")
    if binding.get("approved_scored_dataset_sha256") is not None:
        raise CalibrationBaselineError("approved scored dataset authority mismatch")
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
    validate_calibration_artifact(
        binding, path, thresholds=thresholds, project=project
    )
    return _ValidatedCalibrationBinding(binding)


def validate_calibration_artifact(
    binding: dict,
    path: str | Path,
    *,
    thresholds: dict | None = None,
    project: dict | None = None,
) -> dict:
    """Validate the scientific binding and provenance embedded in an artifact."""
    try:
        artifact_content = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationBaselineError("calibration artifact content is invalid") from exc
    if (
        artifact_content.get("schema_version") != CALIBRATION_BASELINE_SCHEMA_VERSION
        or artifact_content.get("artifact_type") != "calibration_baseline"
    ):
        raise CalibrationBaselineError("calibration artifact envelope is invalid")
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
    _validate_prediction_owner(
        binding.get("protocol_identity"), binding.get("scoring_implementation")
    )
    _validate_authority(dataset, str(binding.get("calibration_authority") or ""))
    try:
        validate_control_metadata(
            _dataset_metadata(dataset),
            project_id=str(binding.get("project_id") or ""),
            approved_digest=str(binding.get("approved_digest") or ""),
            protocol=binding.get("protocol_identity"),
            scoring_implementation=binding.get("scoring_implementation"),
        )
        validate_control_provenance(_controls(dataset), version=2)
    except ControlDataError as exc:
        raise CalibrationBaselineError(str(exc)) from exc
    audit = artifact_content.get("calibration_audit")
    if not isinstance(audit, dict) or object_sha256(audit) != binding.get(
        "calibration_audit_sha256"
    ):
        raise CalibrationBaselineError("calibration artifact audit mismatch")
    parameters_sha256 = _validate_calibrator_lineage(
        dataset=dataset,
        audit=audit,
        protocol_identity=binding.get("protocol_identity"),
        scoring_implementation=binding.get("scoring_implementation"),
        project=project,
    )
    if parameters_sha256 != binding.get("calibration_parameters_sha256"):
        raise CalibrationBaselineError(
            "calibration binding parameters identity mismatch"
        )
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
