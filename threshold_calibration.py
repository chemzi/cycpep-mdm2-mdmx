"""Auditable target-specific threshold calibration.

This module deliberately does not invent a universal cutoff. It converts
positive/negative controls produced by the same tool protocol into a versioned
threshold entry that ``evaluate_battery`` can audit.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable


def _numbers(values: Iterable[float]) -> list[float]:
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _passes(value: float, threshold: float, direction: str) -> bool:
    return value >= threshold if direction == "maximize" else value <= threshold


def _protocol_hash(protocol: dict | None) -> str | None:
    if not protocol:
        return None
    payload = json.dumps(protocol, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def calibrate_threshold(
    *,
    metric: str,
    target_id: str,
    negatives: Iterable[float],
    positives: Iterable[float] = (),
    direction: str = "maximize",
    max_false_positive_rate: float = 0.05,
    tool: str | None = None,
    protocol: dict | None = None,
) -> dict:
    """Choose the highest-recall cutoff subject to an empirical FPR ceiling.

    With no positives, the least-strict cutoff satisfying the FPR ceiling is
    returned and marked ``empirical_null``. With positives, recall is maximized
    under the same constraint and the result is marked ``positive_control``.
    """
    if direction not in {"maximize", "minimize"}:
        raise ValueError("direction must be maximize or minimize")
    if not 0 <= max_false_positive_rate < 1:
        raise ValueError("max_false_positive_rate must be in [0, 1)")
    neg = _numbers(negatives)
    pos = _numbers(positives)
    if not neg:
        raise ValueError("at least one valid negative-control score is required")

    candidates = sorted(set(neg + pos))
    # Add a zero-FP boundary so small control sets can still express alpha=0.
    epsilon = max(1e-12, (max(candidates) - min(candidates)) * 1e-9)
    candidates.append(max(candidates) + epsilon if direction == "maximize" else min(candidates) - epsilon)

    feasible = []
    for cutoff in candidates:
        fp = sum(_passes(value, cutoff, direction) for value in neg)
        fpr = fp / len(neg)
        if fpr <= max_false_positive_rate:
            recall = (
                sum(_passes(value, cutoff, direction) for value in pos) / len(pos)
                if pos else None
            )
            feasible.append((cutoff, fpr, recall))
    if not feasible:  # Defensive; boundary above guarantees feasibility.
        raise RuntimeError("no feasible threshold found")

    if pos:
        best_recall = max(item[2] for item in feasible)
        tied = [item for item in feasible if item[2] == best_recall]
    else:
        tied = feasible
    # Least strict among equally useful thresholds.
    cutoff, observed_fpr, recall = (
        min(tied, key=lambda item: item[0])
        if direction == "maximize"
        else max(tied, key=lambda item: item[0])
    )

    evidence_grade = "positive_control" if pos else "empirical_null"
    return {
        "metric": metric,
        "target_id": target_id,
        "value": cutoff,
        "operator": ">=" if direction == "maximize" else "<=",
        "direction": direction,
        "policy": "max_recall_at_fpr" if pos else "negative_control_fpr",
        "source": f"same-protocol {evidence_grade} calibration",
        "evidence_grade": evidence_grade,
        "calibration_status": "calibrated",
        "n_positive": len(pos),
        "n_negative": len(neg),
        "max_false_positive_rate": max_false_positive_rate,
        "observed_false_positive_rate": observed_fpr,
        "positive_recall": recall,
        "tool": tool,
        "protocol_hash": _protocol_hash(protocol),
    }
