"""Path-independent identity of the existing Prediction scientific executor.

This module composes identities already enforced by the Prediction workers. It
does not locate or probe tools; deployment preflight remains owned by those
workers and the Execution adapter.
"""

from __future__ import annotations

from typing import Any, Mapping

from .boltz_worker import (
    BOLTZ2_CHECKPOINT_SHA256,
    BOLTZ_MODEL_FAMILY,
    BOLTZ_MODEL_ID,
    BOLTZ_VERSION,
)
from .contracts import ContractError, PredictionConfig, object_sha256
from .protocol import PREDICTION_PROTOCOL, protocol_binding
from .rosetta_worker import PYROSETTA_VERSION


EXECUTION_IDENTITY_SCHEMA_VERSION = 1
# Canonical installed distribution version used by the deployed runtime.
PRODIGY_VERSION = "2.4.0"

_IDENTITY_KEYS = frozenset({
    "schema_version",
    "prediction_protocol",
    "colabdesign",
    "af2",
    "boltz",
    "pyrosetta",
    "prodigy",
    "prediction_config",
    "configuration_digest",
})


def _identity_payload(
    config: PredictionConfig, observations: Mapping[str, str] | None = None
) -> dict[str, Any]:
    observed = dict(observations or {})
    af2 = PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]
    boltz = PREDICTION_PROTOCOL["parameters"]["boltz"]
    return {
        "schema_version": EXECUTION_IDENTITY_SCHEMA_VERSION,
        "prediction_protocol": protocol_binding(),
        "colabdesign": {
            "commit": observed.get("colabdesign_commit", config.colabdesign_commit)
        },
        "af2": {
            "model_family": "AlphaFold2",
            "model_ids": [
                f"alphafold2_model_{value}" for value in af2["model_numbers"]
            ],
            "seeds": list(af2["seeds"]),
            "model_numbers": list(af2["model_numbers"]),
            "num_recycles": af2["num_recycles"],
        },
        "boltz": {
            "version": observed.get("boltz_version", BOLTZ_VERSION),
            "model_family": BOLTZ_MODEL_FAMILY,
            "model_id": BOLTZ_MODEL_ID,
            "checkpoint_sha256": observed.get(
                "boltz_checkpoint_sha256", BOLTZ2_CHECKPOINT_SHA256
            ),
            "diffusion_samples": boltz["diffusion_samples"],
        },
        "pyrosetta": {
            "version": observed.get("pyrosetta_version", PYROSETTA_VERSION)
        },
        "prodigy": {"version": observed.get("prodigy_version", PRODIGY_VERSION)},
        "prediction_config": config.to_dict(),
    }


def build_prediction_execution_identity(
    config: PredictionConfig | None = None,
    *,
    observations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the current protocol/tool/model identity without locators."""
    payload = _identity_payload(config or PredictionConfig(), observations)
    return {**payload, "configuration_digest": object_sha256(payload)}


def validate_prediction_execution_identity(
    value: Mapping[str, Any], *, expected: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate an immutable identity and optionally require an exact match."""
    if not isinstance(value, Mapping):
        raise ContractError(
            "prediction_execution_identity_invalid",
            "Prediction execution identity must be an object",
        )
    unknown = sorted(set(value) - _IDENTITY_KEYS)
    missing = sorted(_IDENTITY_KEYS - set(value))
    if unknown or missing:
        raise ContractError(
            "prediction_execution_identity_invalid",
            f"Prediction execution identity has unsupported={unknown}, missing={missing}",
        )
    normalized = {key: value[key] for key in _IDENTITY_KEYS - {"configuration_digest"}}
    digest = value.get("configuration_digest")
    if not isinstance(digest, str) or digest != object_sha256(normalized):
        raise ContractError(
            "prediction_execution_identity_digest_mismatch",
            "Prediction execution identity configuration digest is invalid",
        )
    result = {**normalized, "configuration_digest": digest}
    if expected is not None and result != validate_prediction_execution_identity(expected):
        raise ContractError(
            "prediction_execution_identity_mismatch",
            "Observed Prediction execution identity differs from the approved identity",
        )
    return result


__all__ = [
    "EXECUTION_IDENTITY_SCHEMA_VERSION",
    "PRODIGY_VERSION",
    "build_prediction_execution_identity",
    "validate_prediction_execution_identity",
]
