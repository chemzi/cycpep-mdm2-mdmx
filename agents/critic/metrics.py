"""metrics - split from agents/critic.py (PR6)."""

from __future__ import annotations

import math, statistics
from typing import Any
from .config import LAYER_METRICS

def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None

def _edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]

def _sequence_similarity(left: str, right: str) -> float:
    denominator = max(len(left), len(right))
    if denominator == 0:
        return 1.0
    return 1.0 - (_edit_distance(left, right) / denominator)

def _metric_evidence(record: dict, layer_key: str) -> dict:
    scope, names = LAYER_METRICS[layer_key]
    metrics = record.get("metrics") or {}
    if scope == "global":
        values = metrics.get("global") or {}
        return {name: values.get(name) for name in names if name in values}
    result = {}
    for target_id, target_values in (metrics.get("targets") or {}).items():
        if isinstance(target_values, dict):
            result[target_id] = {
                name: target_values.get(name)
                for name in names if name in target_values
            }
    return result

def _target_metric_summary(records: list[dict], targets: list[str]) -> dict:
    result: dict[str, dict] = {}
    for target_id in targets:
        target_summary = {}
        for metric in (
            "ipsae", "iptm", "dg", "sc", "dsasa", "hotspot_cov", "pose_rmsd"
        ):
            values = []
            for item in records:
                value = _finite_number(
                    (((item["record"].get("metrics") or {}).get("targets") or {})
                     .get(target_id, {}).get(metric))
                )
                if value is not None:
                    values.append(value)
            target_summary[metric] = {"n": len(values), "median": _median(values)}
        result[target_id] = target_summary
    return result
