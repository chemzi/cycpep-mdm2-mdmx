"""record_review - split from agents/critic/report.py (PR6)."""

from __future__ import annotations

from .config import LAYER_ISSUES, LAYER_KEYS
from .errors import CriticContractError
from .integrity import _issue, _layer_has_missing_threshold
from .metrics import _metric_evidence

def _prediction_pending_review(
    issues: dict[str, dict],
    candidate_id: str,
    record: dict,
    battery: dict,
) -> bool:
    """Emit evidence-gap issues for a pending prediction; report completeness."""
    prediction_issues = list(record.get("issues") or [])
    issue_codes = {
        str(item.get("code") or "")
        for item in prediction_issues if isinstance(item, dict)
    }
    missing_evidence = list(battery.get("missing_evidence") or [])
    if "l7_reference_missing" in issue_codes:
        _issue(
            issues,
            code="design_reference_missing",
            severity="high",
            category="design_contract",
            message=(
                "Design did not provide an independent L7 reference backbone; "
                "the candidate must be regenerated in Design before Prediction."
            ),
            candidate_ids=[candidate_id],
            evidence={
                "missing_evidence": [
                    value for value in missing_evidence
                    if value in {"scrmsd", "l7_reference_missing"}
                ],
                "issues": [
                    item for item in prediction_issues
                    if isinstance(item, dict)
                    and item.get("code") == "l7_reference_missing"
                ],
            },
            recommended_action="regenerate_design_reference",
            owner_hint="design",
            blocks_finalization=True,
        )
    remaining_missing = [
        value for value in missing_evidence
        if value not in {"scrmsd", "l7_reference_missing"}
    ]
    remaining_issues = [
        item for item in prediction_issues
        if not isinstance(item, dict)
        or item.get("code") != "l7_reference_missing"
    ]
    if remaining_missing or remaining_issues:
        _issue(
            issues,
            code="prediction_evidence_incomplete",
            severity="high",
            category="operational",
            message="Required Prediction evidence is missing or invalid.",
            candidate_ids=[candidate_id],
            evidence={
                "missing_evidence": remaining_missing,
                "issues": remaining_issues,
            },
            recommended_action="complete_prediction_evidence",
            owner_hint="prediction",
            blocks_finalization=True,
        )
    return bool(remaining_missing or remaining_issues)

def _failed_layer_review(
    issues: dict[str, dict],
    candidate_id: str,
    status: str,
    battery: dict,
    record: dict,
    operationally_incomplete: bool,
) -> None:
    """Emit scientific metric issues for reviewable failed layers."""
    failed_layers = battery.get("failed_layers") or []
    if status == "needs_optimization" and not failed_layers:
        raise CriticContractError(
            "record_status_inconsistent",
            f"{candidate_id} needs optimization but has no failed layer",
        )
    # A null threshold makes the overall Prediction status pending, but it
    # must not hide failures in other layers whose model evidence and gate
    # values are already present.  The layer that owns the null threshold
    # remains a calibration question until that gate is materialized.
    metric_failures_are_reviewable = (
        status == "needs_optimization"
        or (status == "prediction_pending" and not operationally_incomplete)
    )
    missing_thresholds = list(battery.get("missing_thresholds") or [])
    reviewable_failed_layers = [
        layer_key for layer_key in failed_layers
        if not _layer_has_missing_threshold(layer_key, missing_thresholds)
    ] if metric_failures_are_reviewable else []
    for layer_key in reviewable_failed_layers:
        if layer_key not in LAYER_ISSUES:
            raise CriticContractError(
                "record_layer_unknown", f"{candidate_id} has {layer_key!r}"
            )
        code, message, action, owner = LAYER_ISSUES[layer_key]
        _issue(
            issues,
            code=code,
            severity="high",
            category="scientific_metric",
            message=message,
            candidate_ids=[candidate_id],
            evidence={
                "candidate_id": candidate_id,
                "metrics": _metric_evidence(record, layer_key),
            },
            recommended_action=action,
            owner_hint=owner,
            blocks_finalization=True,
        )

def _review_candidate_index(
    item: dict,
    rows_by_id: dict[str, dict],
    issues: dict[str, dict],
) -> None:
    """Emit CandidateIndex presence/sequence issues for one record."""
    candidate_id = item["candidate_id"]
    record = item["record"]
    if candidate_id not in rows_by_id:
        _issue(
            issues,
            code="candidate_index_entry_missing",
            severity="blocker",
            category="operational",
            message="A Prediction record has no matching CandidateIndex row.",
            candidate_ids=[candidate_id],
            evidence={"record_path": str(item["path"])},
            recommended_action="repair_candidate_index",
            owner_hint="design/data",
            blocks_finalization=True,
        )
    record_sequence = str(
        (record.get("candidate") or {}).get("sequence") or ""
    ).strip().upper()
    if not record_sequence:
        raise CriticContractError(
            "record_sequence_missing", f"{candidate_id} record has no sequence"
        )
    row_sequence = str(
        (rows_by_id.get(candidate_id) or {}).get("sequence") or ""
    ).strip().upper()
    if row_sequence and row_sequence != record_sequence:
        _issue(
            issues,
            code="candidate_index_sequence_mismatch",
            severity="blocker",
            category="operational",
            message="CandidateIndex sequence differs from the immutable Prediction record.",
            candidate_ids=[candidate_id],
            evidence={
                "record_sequence": record_sequence,
                "candidate_index_sequence": row_sequence,
            },
            recommended_action="repair_candidate_index",
            owner_hint="design/data",
            blocks_finalization=True,
        )


def _validate_record_battery(
    candidate_id: str,
    status: str,
    record: dict,
    issues: dict[str, dict],
    layer_counts: dict[str, dict[str, int]],
) -> dict:
    """Validate battery consistency and update per-layer pass/fail counts."""
    if status == "invalid":
        _issue(
            issues,
            code="invalid_prediction_artifact",
            severity="blocker",
            category="operational",
            message="Prediction rejected input, provenance, sequence, hash, or geometry.",
            candidate_ids=[candidate_id],
            evidence={"issues": record.get("issues") or []},
            recommended_action="regenerate_invalid_artifact",
            owner_hint="prediction/design",
            blocks_finalization=True,
        )
        for key in LAYER_KEYS:
            layer_counts[key]["missing"] += 1
        return {}
    battery = record.get("battery")
    if not isinstance(battery, dict):
        raise CriticContractError(
            "record_battery_missing", f"{candidate_id} has no metric battery"
        )
    if status in {"finalized", "awaiting_threshold_calibration"} and (
        battery.get("all_layers_pass") is not True
    ):
        raise CriticContractError(
            "record_status_inconsistent",
            f"{candidate_id} status requires all_layers_pass=true",
        )
    if status == "needs_optimization" and battery.get("all_layers_pass") is True:
        raise CriticContractError(
            "record_status_inconsistent",
            f"{candidate_id} needs_optimization conflicts with all_layers_pass=true",
        )
    for key in LAYER_KEYS:
        value = battery.get(key)
        if value is True:
            layer_counts[key]["pass"] += 1
        elif value is False:
            layer_counts[key]["fail"] += 1
        else:
            layer_counts[key]["missing"] += 1
    return battery


def _review_record(
    item: dict,
    rows_by_id: dict[str, dict],
    issues: dict[str, dict],
    layer_counts: dict[str, dict[str, int]],
    unjustified_thresholds: set[str],
    unjustified_candidates: set[str],
) -> None:
    """Review one Prediction record against its CandidateIndex row."""
    candidate_id = item["candidate_id"]
    status = item["status"]
    record = item["record"]
    _review_candidate_index(item, rows_by_id, issues)
    battery = _validate_record_battery(candidate_id, status, record, issues, layer_counts)
    if not battery:
        return
    operationally_incomplete = (
        _prediction_pending_review(issues, candidate_id, record, battery)
        if status == "prediction_pending"
        else False
    )
    _failed_layer_review(
        issues, candidate_id, status, battery, record, operationally_incomplete
    )
    for threshold_key, audit in (battery.get("threshold_audit") or {}).items():
        if isinstance(audit, dict) and audit.get("justified") is False:
            unjustified_thresholds.add(str(threshold_key))
            unjustified_candidates.add(candidate_id)
