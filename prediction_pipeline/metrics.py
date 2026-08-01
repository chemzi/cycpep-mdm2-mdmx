"""Validated metric calculations from raw Prediction artifacts."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

from .contracts import ContractError
from .structures import (
    Structure,
    exact_sequence_chain,
    target_aligned_binder_rmsd,
)


def load_pae(path: str | Path) -> np.ndarray:
    """Load common AlphaFold/ColabDesign PAE JSON or NPZ formats."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ContractError("pae_missing", f"PAE artifact not found: {path}")
    if path.suffix.lower() == ".npz":
        with np.load(path, allow_pickle=False) as data:
            for key in ("pae", "predicted_aligned_error"):
                if key in data:
                    matrix = np.asarray(data[key], dtype=float)
                    break
            else:
                raise ContractError(
                    "pae_key_missing", f"{path} has no pae/predicted_aligned_error array"
                )
    elif path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError("pae_malformed", f"invalid PAE JSON: {path}") from exc
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            payload = payload[0]
        if isinstance(payload, dict):
            value = payload.get("pae")
            if value is None:
                value = payload.get("predicted_aligned_error")
        else:
            value = payload
        if value is None:
            raise ContractError("pae_key_missing", f"{path} has no PAE matrix")
        matrix = np.asarray(value, dtype=float)
    else:
        raise ContractError("pae_format_unsupported", f"unsupported PAE format: {path}")

    if matrix.ndim == 3 and matrix.shape[0] == 1:
        matrix = matrix[0]
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or not matrix.size:
        raise ContractError("pae_shape_invalid", f"PAE must be a non-empty square matrix: {path}")
    if not np.isfinite(matrix).all() or np.any(matrix < 0):
        raise ContractError("pae_value_invalid", f"PAE has negative/non-finite values: {path}")
    return matrix


def _d0(number_of_residues: int) -> float:
    """Protein d0 used by IPSAE v4 (DunbrackLab, 2026-01-03)."""
    length = max(26.0, float(number_of_residues))
    return max(1.0, 1.24 * (length - 15.0) ** (1.0 / 3.0) - 1.8)


def _ipsae_asymmetric(
    pae: np.ndarray,
    chains: np.ndarray,
    align_chain: str,
    scored_chain: str,
    pae_cutoff: float,
) -> dict:
    align_indices = np.flatnonzero(chains == align_chain)
    scored_mask = chains == scored_chain
    scores = []
    valid_counts = []
    for index in align_indices:
        valid = scored_mask & (pae[index] < pae_cutoff)
        count = int(valid.sum())
        valid_counts.append(count)
        if not count:
            scores.append(0.0)
            continue
        d0 = _d0(count)
        transformed = 1.0 / (1.0 + (pae[index, valid] / d0) ** 2.0)
        scores.append(float(transformed.mean()))
    if not scores:
        return {"score": 0.0, "align_index": None, "n0res": 0, "d0res": 1.0}
    best = int(np.argmax(scores))
    count = valid_counts[best]
    return {
        "score": float(scores[best]),
        "align_index": int(align_indices[best]),
        "n0res": count,
        "d0res": _d0(count),
    }


def calculate_ipsae(
    pae: np.ndarray,
    chain_labels: list[str] | np.ndarray,
    chain_a: str,
    chain_b: str,
    pae_cutoff: float,
) -> dict:
    """Calculate official ipSAE (d0res, symmetric max) for one chain pair.

    The implementation follows DunbrackLab/IPSAE v4: for every aligned
    residue, keep inter-chain pairs below the PAE cutoff, calculate a
    residue-specific d0 from the number of retained residues, average the TM
    transform, then take the maximum residue and the maximum direction.
    """
    chains = np.asarray(chain_labels, dtype=str)
    if pae.shape != (len(chains), len(chains)):
        raise ContractError(
            "pae_structure_mismatch",
            f"PAE shape {pae.shape} does not match {len(chains)} structure residues",
        )
    if chain_a == chain_b or chain_a not in chains or chain_b not in chains:
        raise ContractError("ipsae_chain_invalid", "ipSAE requires two present, distinct chains")
    if pae_cutoff <= 0:
        raise ContractError("ipsae_cutoff_invalid", "PAE cutoff must be positive")
    ab = _ipsae_asymmetric(pae, chains, chain_a, chain_b, pae_cutoff)
    ba = _ipsae_asymmetric(pae, chains, chain_b, chain_a, pae_cutoff)
    selected_direction = f"{chain_a}->{chain_b}" if ab["score"] >= ba["score"] else f"{chain_b}->{chain_a}"
    interchain = np.concatenate([
        pae[np.ix_(chains == chain_a, chains == chain_b)].ravel(),
        pae[np.ix_(chains == chain_b, chains == chain_a)].ravel(),
    ])
    return {
        "ipsae": max(ab["score"], ba["score"]),
        "ipsae_asym": {f"{chain_a}->{chain_b}": ab, f"{chain_b}->{chain_a}": ba},
        "selected_direction": selected_direction,
        "pae_cutoff_angstrom": float(pae_cutoff),
        "interchain_pae_median": float(np.median(interchain)),
    }


_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


def parse_prodigy_output(text: str) -> dict:
    """Parse PRODIGY normal or quiet output without accepting ambiguous values."""
    stripped = text.strip()
    if re.fullmatch(_FLOAT, stripped):
        value = float(stripped)
    else:
        patterns = [
            rf"Predicted\s+binding\s+affinity.*?({_FLOAT})\s*kcal",
            rf"(?:binding\s+affinity|delta[_ ]?g|dG)\s*[:=]\s*({_FLOAT})",
            # PRODIGY 2.4 quiet output: "<model_name>  <dG>"
            rf"^[^\s#]+\s+({_FLOAT})\s*$",
        ]
        matches = []
        for pattern in patterns:
            flags = re.IGNORECASE | (
                re.MULTILINE if pattern.startswith("^") else re.DOTALL
            )
            matches.extend(re.findall(pattern, text, flags=flags))
        unique = {float(value) for value in matches}
        if len(unique) != 1:
            raise ContractError(
                "prodigy_parse_failed",
                f"expected one PRODIGY affinity value, found {sorted(unique)}",
            )
        value = unique.pop()
    if not math.isfinite(value):
        raise ContractError("prodigy_value_invalid", "PRODIGY dG is non-finite")
    return {"dg": value, "dg_method": "prodigy"}


def parse_rosetta_interface_output(text: str) -> dict:
    """Parse InterfaceAnalyzer scorefile/table for dSASA and shape complementarity."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[dict[str, str]] = []
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("SCORE:"):
            continue
        headers = line.split()[1:]
        values = lines[index + 1].split()
        if not values or values[0] != "SCORE:" or len(values[1:]) != len(headers):
            continue
        candidates.append(dict(zip(headers, values[1:])))
    if not candidates:
        # Rosetta tracer sometimes prints key/value lines.
        pairs = dict(re.findall(rf"\b([A-Za-z][A-Za-z0-9_]*)\s*[:=]\s*({_FLOAT})", text))
        if pairs:
            candidates = [pairs]
    if not candidates:
        raise ContractError("rosetta_parse_failed", "no Rosetta score row found")

    row = candidates[-1]
    dsasa_raw = next(
        (row[key] for key in ("dSASA_int", "dSASA", "interface_delta_sasa") if key in row),
        None,
    )
    sc_raw = next(
        (row[key] for key in ("sc_value", "SC", "shape_complementarity") if key in row),
        None,
    )
    if dsasa_raw is None or sc_raw is None:
        raise ContractError(
            "rosetta_metric_missing",
            f"Rosetta output lacks dSASA_int or sc_value; keys={sorted(row)}",
        )
    try:
        dsasa, sc = float(dsasa_raw), float(sc_raw)
    except ValueError as exc:
        raise ContractError("rosetta_value_invalid", "Rosetta metric is not numeric") from exc
    if not math.isfinite(dsasa) or dsasa < 0 or not math.isfinite(sc) or not 0 <= sc <= 1:
        raise ContractError(
            "rosetta_value_invalid", f"invalid Rosetta dSASA/sc values: {dsasa}, {sc}"
        )
    result = {"dsasa": dsasa, "sc": sc}
    if "dG_separated" in row:
        try:
            result["rosetta_dg_separated"] = float(row["dG_separated"])
        except ValueError:
            pass
    return result


def pose_convergence(
    predictions: list[dict],
    target_chain: str,
    sequence: str,
    cluster_cutoff: float,
    minimum_predictions: int,
    minimum_predictors: int,
) -> dict:
    """Target-align complex models, then quantify binder-pose agreement."""
    if len(predictions) < minimum_predictions:
        raise ContractError(
            "l6_predictions_insufficient",
            f"L6 needs {minimum_predictions} predictions; received {len(predictions)}",
        )
    identities = []
    for item in predictions:
        predictor = str(item.get("predictor") or "").strip()
        metadata = item.get("metadata_values") or {}
        tool = str(metadata.get("tool") or "").strip()
        model_family = str(metadata.get("model_family") or "").strip()
        revision = str(
            metadata.get("tool_commit")
            or metadata.get("tool_version")
            or ""
        ).strip()
        metadata_seed = metadata.get("seed")
        missing = [
            name for name, value in (
                ("metadata.tool", tool),
                ("metadata.model_family", model_family),
                ("metadata.tool_commit/tool_version", revision),
            )
            if not value
        ]
        if metadata_seed is None:
            missing.append("metadata.seed")
        if missing:
            raise ContractError(
                "l6_predictor_provenance_missing",
                f"L6 prediction lacks {missing}: {item['pdb']['path']}",
            )
        if tool.casefold() != predictor.casefold():
            raise ContractError(
                "l6_predictor_identity_mismatch",
                f"L6 predictor {predictor!r} does not match metadata tool {tool!r}",
            )
        if (
            isinstance(metadata_seed, bool)
            or not isinstance(metadata_seed, int)
            or metadata_seed != item.get("seed")
        ):
            raise ContractError(
                "l6_seed_provenance_mismatch",
                f"L6 seed {item.get('seed')!r} does not match metadata seed "
                f"{metadata_seed!r}",
            )
        identities.append({
            "tool": tool,
            "tool_key": tool.casefold(),
            "model_family": model_family,
            "model_family_key": model_family.casefold(),
            "revision": revision,
            "seed": metadata_seed,
            "pdb_sha256": item["pdb"]["sha256"],
        })

    run_keys = [(item["tool_key"], item["seed"]) for item in identities]
    duplicate_runs = sorted({key for key in run_keys if run_keys.count(key) > 1})
    if duplicate_runs:
        raise ContractError(
            "l6_prediction_duplicate",
            f"duplicate predictor/seed evidence: {duplicate_runs}",
        )
    pdb_hashes = [item["pdb_sha256"] for item in identities]
    duplicate_hashes = sorted({value for value in pdb_hashes if pdb_hashes.count(value) > 1})
    if duplicate_hashes:
        raise ContractError(
            "l6_prediction_duplicate",
            "identical PDB content was registered as multiple L6 predictions: "
            f"{duplicate_hashes}",
        )

    predictors = {item["tool"] for item in identities}
    model_families = {item["model_family"] for item in identities}
    if len({item["model_family_key"] for item in identities}) < minimum_predictors:
        raise ContractError(
            "l6_predictors_insufficient",
            f"L6 needs {minimum_predictors} independent model families; received "
            f"{sorted(model_families)} from tools {sorted(predictors)}",
        )

    for item in predictions:
        structure = item["structure"]
        item["binder_chain"] = item.get("binder_chain") or exact_sequence_chain(
            structure, sequence
        )

    matrix = np.zeros((len(predictions), len(predictions)), dtype=float)
    cross_predictor = []
    for i in range(len(predictions)):
        for j in range(i + 1, len(predictions)):
            value = target_aligned_binder_rmsd(
                predictions[i]["structure"],
                predictions[j]["structure"],
                target_chain,
                predictions[i]["binder_chain"],
                predictions[j]["binder_chain"],
            )
            matrix[i, j] = matrix[j, i] = value
            if (
                identities[i]["model_family_key"]
                != identities[j]["model_family_key"]
            ):
                cross_predictor.append(value)
    if not cross_predictor:
        raise ContractError(
            "l6_cross_predictor_missing", "no cross-model-family pose pair"
        )

    cluster_sizes = (matrix <= cluster_cutoff).sum(axis=1)
    medoid = int(np.argmax(cluster_sizes))
    seed_fraction = float(cluster_sizes[medoid] / len(predictions))
    return {
        "pose_rmsd": float(np.median(cross_predictor)),
        "seed_convergence": seed_fraction,
        "pairwise_rmsd": matrix.round(6).tolist(),
        "cluster_cutoff_angstrom": float(cluster_cutoff),
        "cluster_medoid_index": medoid,
        "prediction_count": len(predictions),
        "predictors": sorted(predictors),
        "model_families": sorted(model_families),
        "predictor_identities": [
            {
                "tool": item["tool"],
                "model_family": item["model_family"],
                "revision": item["revision"],
                "seed": item["seed"],
                "pdb_sha256": item["pdb_sha256"],
            }
            for item in identities
        ],
        "seeds": [item.get("seed") for item in predictions],
    }
