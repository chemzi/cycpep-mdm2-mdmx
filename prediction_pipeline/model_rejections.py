"""Typed Rosetta model-rejection identity and exact-coverage contract."""

from __future__ import annotations

import math

from .contracts import (
    ContractError,
    ROSETTA_MAXIMUM_TERMINAL_DISTANCE_ANGSTROM,
)


def model_identity(entry: dict) -> tuple[str, str, int, str]:
    return (
        entry["predictor"], entry["model_id"], entry["seed"],
        entry["prediction_pdb_sha256"],
    )


def validate_rejection(
    entry: dict, *, label: str, prediction: dict, target_chain: str,
    binder_chain: str, binder_sequence: str, expected_model_id: str,
) -> dict:
    allowed = {
        "predictor", "model_id", "seed", "prediction_pdb_sha256",
        "target_chain", "binder_chain", "binder_sequence", "code",
        "observed_terminal_c_to_n_distance_angstrom",
        "maximum_terminal_c_to_n_distance_angstrom",
    }
    if not isinstance(entry, dict):
        raise ContractError("artifact_entry_type", f"{label} must be an object")
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ContractError("artifact_unknown_keys", f"{label}: {unknown}")
    expected = {
        "predictor": prediction["predictor"],
        "model_id": expected_model_id,
        "seed": prediction["seed"],
        "prediction_pdb_sha256": prediction["pdb"]["sha256"],
        "target_chain": target_chain,
        "binder_chain": binder_chain,
        "binder_sequence": binder_sequence,
        "code": "rosetta_cyclic_bond_open",
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        raise ContractError(
            "rosetta_rejection_binding_mismatch",
            f"{label} does not match its declared prediction and binding",
        )
    observed = entry.get("observed_terminal_c_to_n_distance_angstrom")
    maximum = entry.get("maximum_terminal_c_to_n_distance_angstrom")
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in (observed, maximum)
        )
        or float(maximum) != ROSETTA_MAXIMUM_TERMINAL_DISTANCE_ANGSTROM
        or float(observed) <= float(maximum)
    ):
        raise ContractError(
            "rosetta_rejection_geometry_invalid", f"{label} has invalid geometry"
        )
    return {
        **entry,
        "observed_terminal_c_to_n_distance_angstrom": float(observed),
        "maximum_terminal_c_to_n_distance_angstrom": float(maximum),
    }


def validate_exact_coverage(
    target_id: str, declared: list[tuple[str, str, int, str]],
    outputs: list[dict], rejections: list[dict],
) -> None:
    successful = [model_identity(item) for item in outputs]
    rejected = [model_identity(item) for item in rejections]
    declared_set, successful_set, rejected_set = map(
        set, (declared, successful, rejected)
    )
    if (
        len(declared_set) != len(declared)
        or len(successful_set) != len(successful)
        or len(rejected_set) != len(rejected)
        or successful_set & rejected_set
        or successful_set | rejected_set != declared_set
    ):
        raise ContractError(
            "rosetta_coverage_mismatch",
            f"{target_id} Rosetta outputs/rejections must XOR-cover every model once",
        )
