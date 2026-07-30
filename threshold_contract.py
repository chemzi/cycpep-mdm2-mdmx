"""Canonical Research threshold contract and evidence-aware merge helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


THRESHOLD_KEY_ALIASES = {
    "L4_ring_closure": "L4_nc_term_dist",
    "L6_pose_convergence": "L6_pose_rmsd",
}

THRESHOLD_FIELDS = (
    "value",
    "operator",
    "unit",
    "source",
    "source_pmid",
    "evidence_quote",
    "evidence_grade",
    "quote_verified",
    "calibration_status",
    "method",
    "applicable_targets",
    "reason_unavailable",
)

_EVIDENCE_RANK = {
    "unavailable": 0,
    "team_provisional": 10,
    "design_rule": 20,
    "empirical_null": 30,
    "positive_control": 40,
    "paper_explicit": 50,
}


def canonical_threshold_key(key: str) -> str:
    return THRESHOLD_KEY_ALIASES.get(str(key), str(key))


def normalize_threshold_entry(entry: Any, *, applicable_targets=None) -> dict:
    raw = deepcopy(entry) if isinstance(entry, dict) else {}
    grade = raw.get("evidence_grade") or raw.get("grade") or "unavailable"
    value = raw.get("value")
    if value is None and grade != "unavailable":
        grade = "unavailable"
    result = {
        "value": value,
        "operator": raw.get("operator"),
        "unit": raw.get("unit"),
        "source": raw.get("source"),
        "source_pmid": str(raw["source_pmid"]) if raw.get("source_pmid") else None,
        "evidence_quote": raw.get("evidence_quote"),
        "evidence_grade": grade,
        "quote_verified": bool(raw.get("quote_verified", False)),
        "calibration_status": raw.get("calibration_status") or (
            "unavailable" if value is None else "pending"
        ),
        "method": raw.get("method"),
        "applicable_targets": list(
            raw.get("applicable_targets")
            if isinstance(raw.get("applicable_targets"), (list, tuple))
            else applicable_targets or []
        ),
        "reason_unavailable": raw.get("reason_unavailable"),
    }
    # Preserve evaluator/protocol extensions without weakening the core contract.
    for key, item in raw.items():
        if key not in result and key not in {"grade"}:
            result[key] = item
    if value is None:
        result["calibration_status"] = "unavailable"
        result["evidence_grade"] = "unavailable"
    return result


def threshold_priority(entry: Any) -> tuple:
    item = normalize_threshold_entry(entry)
    calibrated = item.get("calibration_status") in {"calibrated", "validated", "complete"}
    verified_explicit = (
        item.get("evidence_grade") == "paper_explicit"
        and item.get("quote_verified")
        and bool(item.get("source_pmid"))
        and bool(item.get("evidence_quote"))
        and item.get("value") is not None
    )
    completeness = sum(
        bool(item.get(key))
        for key in ("source_pmid", "evidence_quote", "quote_verified")
    ) + int(item.get("value") is not None)
    return (
        int(calibrated),
        _EVIDENCE_RANK.get(item.get("evidence_grade"), -1),
        int(verified_explicit),
        completeness,
    )


def normalize_thresholds(thresholds: Any, *, applicable_targets=None) -> tuple[dict, dict]:
    """Return canonical keys and an auditable record of duplicate resolution."""
    if not isinstance(thresholds, dict):
        thresholds = {}
    normalized: dict[str, dict] = {}
    sources: dict[str, str] = {}
    conflicts = []
    for raw_key, raw_entry in thresholds.items():
        if str(raw_key).startswith("_"):
            continue
        key = canonical_threshold_key(raw_key)
        candidate = normalize_threshold_entry(raw_entry, applicable_targets=applicable_targets)
        if key not in normalized:
            normalized[key] = candidate
            sources[key] = str(raw_key)
            continue
        current = normalized[key]
        current_priority = threshold_priority(current)
        candidate_priority = threshold_priority(candidate)
        if candidate_priority > current_priority:
            winner, dropped = candidate, current
            winner_source, dropped_source = str(raw_key), sources[key]
            reason = "higher_evidence_or_verification_priority"
            normalized[key] = candidate
            sources[key] = str(raw_key)
        else:
            winner, dropped = current, candidate
            winner_source, dropped_source = sources[key], str(raw_key)
            reason = (
                "equal_priority_kept_first"
                if candidate_priority == current_priority
                else "existing_has_higher_evidence_or_verification_priority"
            )
        conflicts.append({
            "canonical_key": key,
            "winner_source_key": winner_source,
            "dropped_source_key": dropped_source,
            "winner_priority": list(threshold_priority(winner)),
            "dropped_priority": list(threshold_priority(dropped)),
            "reason": reason,
        })
    return normalized, {
        "input_keys": [str(key) for key in thresholds if not str(key).startswith("_")],
        "canonical_keys": list(normalized),
        "conflicts": conflicts,
    }


def merge_thresholds(existing: Any, incoming: Any) -> tuple[dict, dict]:
    """Evidence-aware merge; lower-grade cache data cannot replace stronger state."""
    current, current_audit = normalize_thresholds(existing)
    additions, incoming_audit = normalize_thresholds(incoming)
    overwritten = []
    skipped = []
    reasons = {}
    for key, candidate in additions.items():
        if key not in current:
            current[key] = candidate
            overwritten.append(key)
            reasons[key] = "added_from_cache"
            continue
        if threshold_priority(candidate) > threshold_priority(current[key]):
            current[key] = candidate
            overwritten.append(key)
            reasons[key] = "cache_has_higher_evidence_or_calibration_priority"
        else:
            skipped.append(key)
            reasons[key] = "state_has_equal_or_higher_evidence_or_calibration_priority"
    return current, {
        "cache_keys": list(additions),
        "final_keys": list(current),
        "overwritten": overwritten,
        "skipped": skipped,
        "conflict_reasons": reasons,
        "state_normalization": current_audit,
        "cache_normalization": incoming_audit,
    }
