"""Prediction artifact reuse/completion policy owned by Execution."""

from __future__ import annotations

import json
from pathlib import Path

from core.protocol import ProtocolError
from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import ContractError, SCHEMA_VERSION
from prediction_pipeline.protocol import (
    PREDICTION_PROTOCOL,
    validate_execution_compatibility,
)

from .contracts import ExecutionContractError


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionContractError(
            f"{label}_invalid", f"cannot read {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise ExecutionContractError(
            f"{label}_invalid", f"{label} must be a JSON object"
        )
    return value


def artifact_bundle_complete(
    path: Path,
    required_targets: list[str],
    expected_execution_identity: dict | None = None,
) -> bool:
    """Return whether a bundle is complete and reusable by the active protocol."""
    if not path.is_file():
        return False
    try:
        raw = _json_object(path, "artifact_bundle")
        validate_execution_compatibility(raw)
        loaded = (
            load_artifact_bundle(
                path,
                candidate_id=str(raw.get("candidate_id") or ""),
                sequence=str(raw.get("sequence") or ""),
                required_targets=tuple(required_targets),
            )
            if raw.get("schema_version") == SCHEMA_VERSION
            else None
        )
        if expected_execution_identity is not None:
            identity_path = path.with_name("execution_identity.json")
            if (
                not identity_path.is_file()
                or _json_object(identity_path, "prediction execution identity")
                != expected_execution_identity
            ):
                return False
        global_values = raw.get("global") or {}
        if not global_values.get("monomer_predictions"):
            return False
        if not global_values.get("post_relax_pdb") or not global_values.get(
            "post_relax_metadata"
        ):
            return False
        expected_af2_seeds = set(
            PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["seeds"]
        )
        targets = raw.get("targets") or {}
        for target_id in required_targets:
            values = targets.get(target_id) or {}
            loaded_values = (
                loaded.target_artifacts[target_id] if loaded is not None else values
            )
            predictions = values.get("complex_predictions") or []
            af2_seeds = {
                prediction.get("seed")
                for prediction in predictions
                if prediction.get("predictor") == "ColabDesign"
                and isinstance(prediction.get("seed"), int)
            }
            if not expected_af2_seeds.issubset(af2_seeds):
                return False
            if not any(
                prediction.get("predictor") == "Boltz" for prediction in predictions
            ):
                return False
            if len(values.get("prodigy_outputs") or []) != len(predictions):
                return False
            if (
                len(loaded_values.get("rosetta_outputs") or [])
                + len(loaded_values.get("rosetta_rejections") or [])
                != len(predictions)
            ):
                return False
    except (ContractError, ExecutionContractError, ProtocolError):
        return False
    return True
