"""Read-only soft desirability / relative-ranking view (v3 P0-C D4).

Metrics that cannot be credibly calibrated must stay out of hard scientific
clearance; this helper exposes them (and all metrics) as a clearly-labelled
soft view: a 0..1 desirability relative to the current threshold, the
``calibration_status``, and whether the metric is hard-eligible under the
battery's justification rules.  It never changes ``evaluate_battery`` or
``competition_clearance``.
"""

from __future__ import annotations

from typing import Any

from project_config import target_value, threshold_for_target
from threshold_calibration import METRIC_SPECS

# Mirrors battery_evaluation._threshold_is_justified on public fields only
# (the architecture gate forbids importing that private helper).  Keep the two
# definitions in sync when clearance rules change.
_CREDIBLE_GRADES = {
    "paper_explicit", "method_explicit", "design_rule",
    "field_consensus", "positive_control", "empirical_null",
}
_CALIBRATED_STATUSES = {"calibrated", "validated", "complete"}


def _hard_eligible(entry: dict) -> tuple[bool, str]:
    """Whether a threshold entry may support hard scientific clearance."""
    if entry.get("value") is None:
        return False, "missing_value"
    if not str(entry.get("source") or "").strip():
        return False, "missing_source"
    status = str(entry.get("calibration_status") or "").casefold()
    if status in _CALIBRATED_STATUSES:
        return True, "calibrated"
    grade = str(entry.get("evidence_grade") or entry.get("grade") or "").casefold()
    if grade in _CREDIBLE_GRADES:
        return True, grade
    return False, grade or status or "ungraded"


def _desirability(value: Any, threshold: Any, direction: str) -> float | None:
    """Closeness to the current gate, normalized to [0, 1].

    Maximize: ``value / threshold`` capped at 1.  Minimize: ``threshold / value``
    capped at 1.  Returns None when either side is missing or non-positive, so a
    soft view never fabricates a score.
    """
    try:
        number = float(value)
        cutoff = float(threshold)
    except (TypeError, ValueError):
        return None
    if direction == "maximize":
        if cutoff <= 0:
            return None
        return max(0.0, min(1.0, number / cutoff))
    if number <= 0:
        return None
    return max(0.0, min(1.0, cutoff / number))


def soft_desirability(
    candidate: dict,
    thresholds: dict | None = None,
    target_ids: tuple[str, ...] | tuple = (),
) -> dict:
    """Return a per-metric soft view for a candidate.

    Output::

        {
          "metrics": {
            "<metric>[:<target>]": {
              "value": ..., "desirability": ..., "calibration_status": ...,
              "hard_eligible": bool, "reason": str
            }, ...
          },
          "hard_eligible_metrics": [...],
          "soft_only_metrics": [...]
        }

    ``hard_eligible`` mirrors the battery clearance justification; ``soft_only``
    lists metrics that are NOT hard-eligible and may only be used for
    desirability / relative ranking.
    """
    thresholds = thresholds or {}
    targets = tuple(str(item) for item in target_ids)
    view: dict[str, dict[str, Any]] = {}
    for key, spec in METRIC_SPECS.items():
        scopes = targets if spec["scope"] == "target" else (None,)
        for scope in scopes:
            entry = threshold_for_target(thresholds, key, scope)
            label = f"{key}:{scope}" if scope else key
            if scope is not None:
                value = target_value(candidate, scope, spec["metric"])
            else:
                from project_config import global_value
                value = global_value(candidate, spec["metric"])
            justified, reason = _hard_eligible(entry)
            view[label] = {
                "value": value,
                "desirability": _desirability(value, entry.get("value"), spec["direction"]),
                "calibration_status": entry.get("calibration_status") or "unavailable",
                "hard_eligible": justified,
                "reason": reason,
            }
    hard_eligible = sorted(key for key, item in view.items() if item["hard_eligible"])
    soft_only = sorted(key for key, item in view.items() if not item["hard_eligible"])
    return {
        "metrics": view,
        "hard_eligible_metrics": hard_eligible,
        "soft_only_metrics": soft_only,
    }
