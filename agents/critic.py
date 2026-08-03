"""Production Critic for audited Prediction handoffs.

Critic interprets complete Prediction records and emits structured feedback for
Planner.  It never changes thresholds, removes candidates, or starts heavy
tools.  ``review()`` is pure; ``run()`` additionally writes an immutable-style
report and records an idempotent State/Evidence summary.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State  # noqa: E402
from prediction_pipeline.contracts import (  # noqa: E402
    file_sha256,
    object_sha256,
)


CRITIC_VERSION = "1.1.0"
REPORT_SCHEMA_VERSION = 1
ALLOWED_STATUSES = {
    "finalized",
    "awaiting_threshold_calibration",
    "prediction_pending",
    "needs_optimization",
    "invalid",
}
LAYER_KEYS = tuple(f"l{number}_pass" for number in range(1, 8))
SEVERITY_RANK = {"blocker": 0, "high": 1, "medium": 2, "info": 3}

LAYER_ISSUES = {
    "l1_pass": (
        "l1_monomer_quality_low",
        "Monomer structural confidence failed the configured L1 gate.",
        "improve_monomer_quality",
        "design",
    ),
    "l2_pass": (
        "l2_interface_confidence_low",
        "One or more required targets failed the ipSAE interface-confidence gate.",
        "iterate_interface_design",
        "design",
    ),
    "l3_pass": (
        "l3_interface_physics_low",
        "One or more required targets failed a physical interface gate.",
        "iterate_interface_physics",
        "design",
    ),
    "l4_pass": (
        "l4_cyclization_geometry_failed",
        "Pre/post-relax cyclic geometry failed the configured L4 gate.",
        "repair_cyclization_geometry",
        "design",
    ),
    "l5_pass": (
        "l5_hotspot_coverage_low",
        "Predicted binding poses do not sufficiently cover reviewed hotspots.",
        "retarget_reviewed_hotspots",
        "design",
    ),
    "l6_pass": (
        "l6_ensemble_convergence_low",
        "Independent predictor/model poses do not satisfy the convergence gate.",
        "improve_pose_robustness",
        "design",
    ),
    "l7_pass": (
        "l7_design_consistency_low",
        "Predicted monomer is inconsistent with the Design backbone.",
        "improve_design_consistency",
        "design",
    ),
}

LAYER_METRICS = {
    "l1_pass": ("global", ("plddt",)),
    "l2_pass": ("targets", ("ipsae",)),
    "l3_pass": ("targets", ("dg", "dg_method", "sc", "dsasa")),
    "l4_pass": (
        "global",
        ("nc_distance_pre", "nc_distance_post", "post_relax_backbone_rmsd"),
    ),
    "l5_pass": ("targets", ("hotspot_cov", "site_consistency")),
    "l6_pass": ("targets", ("pose_rmsd", "seed_convergence")),
    "l7_pass": ("global", ("scrmsd",)),
}

ACTION_DEFAULTS = {
    "regenerate_invalid_artifact": ("prediction/design", "P0", False),
    "complete_prediction_evidence": ("prediction", "P0", False),
    "regenerate_design_reference": ("design", "P0", False),
    "improve_monomer_quality": ("design", "P1", False),
    "iterate_interface_design": ("design", "P1", False),
    "iterate_interface_physics": ("design", "P1", False),
    "repair_cyclization_geometry": ("design", "P0", False),
    "retarget_reviewed_hotspots": ("design", "P1", False),
    "improve_pose_robustness": ("design", "P1", False),
    "improve_design_consistency": ("design", "P1", False),
    "calibrate_thresholds": ("research", "P2", True),
    "deduplicate_candidates": ("design", "P1", False),
    "increase_sequence_diversity": ("design", "P2", False),
    "generate_review_cohort": ("design", "P2", False),
    "repair_candidate_index": ("design/data", "P0", False),
}


class CriticContractError(ValueError):
    """Prediction handoff cannot be trusted by Critic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CriticConfig:
    min_cohort_for_distribution: int = 3
    low_diversity_median_similarity: float = 0.80
    max_issue_examples: int = 20

    def __post_init__(self) -> None:
        if self.min_cohort_for_distribution < 1:
            raise CriticContractError(
                "critic_config_invalid", "minimum cohort must be positive"
            )
        if not 0 <= self.low_diversity_median_similarity <= 1:
            raise CriticContractError(
                "critic_config_invalid", "similarity threshold must be in [0, 1]"
            )
        if self.max_issue_examples < 1:
            raise CriticContractError(
                "critic_config_invalid", "max_issue_examples must be positive"
            )


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CriticContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CriticContractError(
            f"{label}_malformed", f"invalid JSON in {path}"
        ) from exc
    if not isinstance(value, dict):
        raise CriticContractError(f"{label}_type", f"{label} must be an object")
    return value


def _resolve_path(raw: Any, base: Path, label: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise CriticContractError(f"{label}_missing", f"missing {label} path")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _load_records(handoff_path: Path) -> tuple[dict, list[dict]]:
    handoff = _json_object(handoff_path, "prediction_handoff")
    if not str(handoff.get("run_id") or "").strip():
        raise CriticContractError("handoff_run_id_missing", "handoff has no run_id")
    if not str(handoff.get("pipeline_version") or "").strip():
        raise CriticContractError(
            "handoff_pipeline_version_missing", "handoff has no pipeline_version"
        )
    required_targets = handoff.get("required_targets")
    if (
        not isinstance(required_targets, list)
        or not required_targets
        or any(not str(value).strip() for value in required_targets)
    ):
        raise CriticContractError(
            "handoff_targets_invalid", "handoff required_targets must be non-empty"
        )
    categories = handoff.get("categories")
    if not isinstance(categories, dict):
        raise CriticContractError(
            "handoff_categories_invalid", "handoff categories must be an object"
        )
    unknown = sorted(set(categories) - ALLOWED_STATUSES)
    if unknown:
        raise CriticContractError(
            "handoff_status_unknown", f"unknown handoff categories: {unknown}"
        )

    records: list[dict] = []
    seen: set[str] = set()
    for status in sorted(categories):
        entries = categories[status]
        if not isinstance(entries, list):
            raise CriticContractError(
                "handoff_category_type", f"handoff category {status} must be a list"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise CriticContractError(
                    "handoff_entry_type", f"handoff category {status} has a non-object"
                )
            candidate_id = str(entry.get("candidate_id") or "").strip()
            if not candidate_id or candidate_id in seen:
                raise CriticContractError(
                    "handoff_candidate_duplicate",
                    f"missing or duplicate candidate in handoff: {candidate_id!r}",
                )
            seen.add(candidate_id)
            record_path = _resolve_path(
                entry.get("record_path"), handoff_path.parent, "record"
            )
            declared_sha = str(entry.get("record_sha256") or "").strip().lower()
            if len(declared_sha) != 64:
                raise CriticContractError(
                    "record_hash_missing", f"{candidate_id} has no full record SHA-256"
                )
            observed_sha = file_sha256(record_path)
            if observed_sha != declared_sha:
                raise CriticContractError(
                    "record_hash_mismatch",
                    f"{candidate_id} record SHA-256 differs from handoff",
                )
            record = _json_object(record_path, "prediction_record")
            record_candidate = str(
                (record.get("candidate") or {}).get("candidate_id") or ""
            ).strip()
            if record_candidate != candidate_id:
                raise CriticContractError(
                    "record_candidate_mismatch",
                    f"handoff {candidate_id} points to record {record_candidate!r}",
                )
            if record.get("status") != status:
                raise CriticContractError(
                    "record_status_mismatch",
                    f"{candidate_id} handoff status {status!r} differs from record",
                )
            if record.get("run_id") != handoff["run_id"]:
                raise CriticContractError(
                    "record_run_mismatch", f"{candidate_id} belongs to another run"
                )
            if record.get("pipeline_version") != handoff["pipeline_version"]:
                raise CriticContractError(
                    "record_pipeline_mismatch",
                    f"{candidate_id} belongs to another pipeline version",
                )
            records.append({
                "candidate_id": candidate_id,
                "status": status,
                "path": record_path,
                "sha256": observed_sha,
                "record": record,
            })
    if not records:
        raise CriticContractError("handoff_empty", "handoff contains no candidates")
    return handoff, records


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


def _diversity_summary(records: list[dict]) -> dict:
    by_sequence: dict[str, list[str]] = defaultdict(list)
    for item in records:
        sequence = str(
            (item["record"].get("candidate") or {}).get("sequence") or ""
        ).upper()
        by_sequence[sequence].append(item["candidate_id"])
    sequences = list(by_sequence)
    similarities = [
        _sequence_similarity(sequences[left], sequences[right])
        for left in range(len(sequences))
        for right in range(left + 1, len(sequences))
    ]
    duplicates = {
        sequence: sorted(candidate_ids)
        for sequence, candidate_ids in by_sequence.items()
        if sequence and len(candidate_ids) > 1
    }
    return {
        "candidate_count": len(records),
        "unique_sequence_count": len(sequences),
        "unique_fraction": len(sequences) / len(records) if records else 0.0,
        "duplicate_sequences": duplicates,
        "pairwise_similarity_method": "1-normalized_levenshtein_distance",
        "pairwise_similarity_n": len(similarities),
        "pairwise_similarity_median": _median(similarities),
        "pairwise_similarity_max": max(similarities) if similarities else None,
    }


def _route_summary(records: list[dict], rows_by_id: dict[str, dict]) -> dict:
    values: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        row = rows_by_id.get(item["candidate_id"]) or {}
        grouped[str(row.get("source_route") or "unknown")].append(item)
    for route, items in sorted(grouped.items()):
        failed_layers = Counter()
        for item in items:
            battery = item["record"].get("battery") or {}
            failed_layers.update(battery.get("failed_layers") or [])
        values[route] = {
            "candidate_count": len(items),
            "status_counts": dict(sorted(Counter(
                item["status"] for item in items
            ).items())),
            "failed_layer_counts": dict(sorted(failed_layers.items())),
        }
    return values


def _issue(
    issues: dict[str, dict],
    *,
    code: str,
    severity: str,
    category: str,
    message: str,
    candidate_ids: list[str] | None = None,
    evidence: Any = None,
    recommended_action: str,
    owner_hint: str,
    blocks_finalization: bool,
) -> None:
    current = issues.get(code)
    if current is None:
        current = {
            "code": code,
            "severity": severity,
            "category": category,
            "message": message,
            "candidate_ids": set(),
            "evidence": [],
            "recommended_action": recommended_action,
            "owner_hint": owner_hint,
            "blocks_finalization": blocks_finalization,
        }
        issues[code] = current
    current["candidate_ids"].update(candidate_ids or [])
    if evidence is not None:
        current["evidence"].append(evidence)


def _finalize_issues(issues: dict[str, dict], config: CriticConfig) -> list[dict]:
    result = []
    for value in issues.values():
        item = dict(value)
        item["candidate_ids"] = sorted(item["candidate_ids"])
        item["evidence"] = item["evidence"][:config.max_issue_examples]
        result.append(item)
    return sorted(result, key=lambda item: (SEVERITY_RANK[item["severity"]], item["code"]))


def _recommendations(issues: list[dict]) -> list[dict]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for issue in issues:
        grouped[issue["recommended_action"]].append(issue["code"])
    result = []
    for action, reason_codes in grouped.items():
        owner, priority, approval_required = ACTION_DEFAULTS[action]
        result.append({
            "action": action,
            "owner_hint": owner,
            "priority": priority,
            "reason_codes": sorted(reason_codes),
            "approval_required": approval_required,
        })
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    return sorted(result, key=lambda item: (
        priority_rank[item["priority"]], item["action"]
    ))


def review(
    *,
    handoff_path: str | Path,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    config: CriticConfig | None = None,
) -> dict:
    """Purely review one immutable Prediction handoff and its records."""
    config = config or CriticConfig()
    handoff_path = Path(handoff_path).expanduser().resolve()
    handoff, records = _load_records(handoff_path)
    state = dict(state if state is not None else State.load())
    candidate_rows = list(
        candidate_rows if candidate_rows is not None else CandidateIndex.load()
    )
    if state.get("project_id") and handoff.get("project_id") and (
        state["project_id"] != handoff["project_id"]
    ):
        raise CriticContractError(
            "critic_project_mismatch", "State and Prediction handoff project IDs differ"
        )

    rows_by_id: dict[str, dict] = {}
    for row in candidate_rows:
        candidate_id = str(row.get("candidate_id") or "").strip()
        if candidate_id in rows_by_id:
            raise CriticContractError(
                "candidate_index_duplicate", f"duplicate CandidateIndex row {candidate_id}"
            )
        if candidate_id:
            rows_by_id[candidate_id] = row

    issues: dict[str, dict] = {}
    layer_counts = {key: {"pass": 0, "fail": 0, "missing": 0} for key in LAYER_KEYS}
    unjustified_thresholds: set[str] = set()
    unjustified_candidates: set[str] = set()
    for item in records:
        candidate_id = item["candidate_id"]
        status = item["status"]
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
        battery = record.get("battery")
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
            continue
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

        if status == "prediction_pending":
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

        failed_layers = battery.get("failed_layers") or []
        if status == "needs_optimization" and not failed_layers:
            raise CriticContractError(
                "record_status_inconsistent",
                f"{candidate_id} needs optimization but has no failed layer",
            )
        for layer_key in failed_layers if status == "needs_optimization" else []:
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

        for threshold_key, audit in (battery.get("threshold_audit") or {}).items():
            if isinstance(audit, dict) and audit.get("justified") is False:
                unjustified_thresholds.add(str(threshold_key))
                unjustified_candidates.add(candidate_id)

    if unjustified_thresholds:
        _issue(
            issues,
            code="threshold_calibration_pending",
            severity="medium",
            category="calibration",
            message="One or more selection thresholds remain provisional or uncalibrated.",
            candidate_ids=sorted(unjustified_candidates),
            evidence={"threshold_keys": sorted(unjustified_thresholds)},
            recommended_action="calibrate_thresholds",
            owner_hint="research",
            blocks_finalization=True,
        )

    diversity = _diversity_summary(records)
    if diversity["duplicate_sequences"]:
        _issue(
            issues,
            code="duplicate_sequences",
            severity="high",
            category="diversity",
            message="Different candidate IDs carry identical peptide sequences.",
            candidate_ids=sorted({
                candidate_id
                for values in diversity["duplicate_sequences"].values()
                for candidate_id in values
            }),
            evidence={"duplicates": diversity["duplicate_sequences"]},
            recommended_action="deduplicate_candidates",
            owner_hint="design",
            blocks_finalization=False,
        )
    if len(records) < config.min_cohort_for_distribution:
        _issue(
            issues,
            code="cohort_too_small",
            severity="info",
            category="cohort",
            message="Cohort is too small for route or distribution-level conclusions.",
            candidate_ids=[item["candidate_id"] for item in records],
            evidence={
                "candidate_count": len(records),
                "minimum": config.min_cohort_for_distribution,
            },
            recommended_action="generate_review_cohort",
            owner_hint="design",
            blocks_finalization=False,
        )
    elif (
        diversity["pairwise_similarity_median"] is not None
        and diversity["pairwise_similarity_median"]
        >= config.low_diversity_median_similarity
    ):
        _issue(
            issues,
            code="low_sequence_diversity",
            severity="medium",
            category="diversity",
            message="Median pairwise sequence similarity is high for this cohort.",
            candidate_ids=[item["candidate_id"] for item in records],
            evidence={
                "median_similarity": diversity["pairwise_similarity_median"],
                "threshold": config.low_diversity_median_similarity,
                "method": diversity["pairwise_similarity_method"],
            },
            recommended_action="increase_sequence_diversity",
            owner_hint="design",
            blocks_finalization=False,
        )

    finalized_issues = _finalize_issues(issues, config)
    statuses = Counter(item["status"] for item in records)
    if any(issue["severity"] == "blocker" for issue in finalized_issues):
        verdict = "blocked"
    elif any(
        issue["severity"] == "high"
        and issue["category"] in {
            "operational", "design_contract", "scientific_metric", "diversity"
        }
        for issue in finalized_issues
    ):
        verdict = "iterate"
    elif any(issue["severity"] == "medium" for issue in finalized_issues):
        verdict = "review"
    else:
        verdict = "clear"
    passed = verdict == "clear"
    issue_counts = dict(sorted(Counter(
        issue["severity"] for issue in finalized_issues
    ).items()))
    summary = (
        f"Critic reviewed {len(records)} candidate(s): verdict={verdict}; "
        f"statuses={dict(sorted(statuses.items()))}; issues={issue_counts}."
    )

    row_snapshot = [
        {
            "candidate_id": item["candidate_id"],
            "record_sequence": (
                item["record"].get("candidate") or {}
            ).get("sequence"),
            "candidate_index_present": item["candidate_id"] in rows_by_id,
            "candidate_index_sequence": (
                rows_by_id.get(item["candidate_id"]) or {}
            ).get("sequence"),
            "source_route": (rows_by_id.get(item["candidate_id"]) or {}).get(
                "source_route"
            ),
        }
        for item in sorted(records, key=lambda value: value["candidate_id"])
    ]
    input_digest = object_sha256({
        "prediction_handoff_path": str(handoff_path),
        "handoff_sha256": file_sha256(handoff_path),
        "state_project_id": state.get("project_id"),
        "thresholds": state.get("thresholds") or {},
        "candidate_rows": row_snapshot,
        "config": asdict(config),
        "critic_version": CRITIC_VERSION,
    })
    report_id = f"critic_{input_digest[:12]}"
    recommendations = _recommendations(finalized_issues)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "critic_version": CRITIC_VERSION,
        "report_id": report_id,
        "input_digest": input_digest,
        "source": {
            "prediction_handoff": str(handoff_path),
            "prediction_handoff_sha256": file_sha256(handoff_path),
            "prediction_run_id": handoff["run_id"],
            "prediction_pipeline_version": handoff["pipeline_version"],
            "project_id": handoff.get("project_id"),
            "required_targets": list(handoff["required_targets"]),
            "record_count": len(records),
        },
        "verdict": verdict,
        "passed": passed,
        "summary": summary,
        "issue_counts": issue_counts,
        "issues": finalized_issues,
        "metrics_snapshot": {
            "status_counts": dict(sorted(statuses.items())),
            "layer_counts": layer_counts,
            "target_metrics": _target_metric_summary(
                records, list(handoff["required_targets"])
            ),
            "route_performance": _route_summary(records, rows_by_id),
            "diversity": diversity,
            "thresholds_unjustified": sorted(unjustified_thresholds),
        },
        "recommendations": recommendations,
        "planner_handoff": {
            "critic_report_id": report_id,
            "issue_codes": [issue["code"] for issue in finalized_issues],
            "recommended_actions": [item["action"] for item in recommendations],
            "policy_constraints": [
                "do_not_change_thresholds_automatically",
                "do_not_delete_candidates_automatically",
                "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
                "reuse_complete_prediction_evidence",
            ],
        },
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(
    *,
    handoff_path: str | Path,
    output_path: str | Path | None = None,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    config: CriticConfig | None = None,
) -> dict:
    """Review, persist the report, and idempotently update Critic state/evidence."""
    handoff_path = Path(handoff_path).expanduser().resolve()
    state = dict(state if state is not None else State.load())
    candidate_rows = list(
        candidate_rows if candidate_rows is not None else CandidateIndex.load()
    )
    report = review(
        handoff_path=handoff_path,
        state=state,
        candidate_rows=candidate_rows,
        config=config,
    )
    if output_path is None:
        output_path = (
            handoff_path.parent / "critic" / report["report_id"] / "critic_report.json"
        )
    output_path = Path(output_path).expanduser().resolve()
    _atomic_json(output_path, report)
    report_sha = file_sha256(output_path)
    summary = {
        "critic_version": CRITIC_VERSION,
        "report_id": report["report_id"],
        "report_path": str(output_path),
        "report_sha256": report_sha,
        "prediction_run_id": report["source"]["prediction_run_id"],
        "verdict": report["verdict"],
        "passed": report["passed"],
        "issue_counts": report["issue_counts"],
        "recommendation_count": len(report["recommendations"]),
    }
    State.update({"phase": "critic", "critic": summary})
    history = State.load().get("iteration_history") or []
    if not any(
        item.get("agent") == "critic"
        and (item.get("summary") or {}).get("report_id") == report["report_id"]
        for item in history
    ):
        State.append_history({"phase": "critic", "agent": "critic", "summary": summary})
    if not any(
        entry.get("event_type") == "critic_review"
        and entry.get("report_id") == report["report_id"]
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.critic_review(
            issues=report["issues"],
            passed=report["passed"],
            summary=report["summary"],
            recommendation=json.dumps(
                report["recommendations"], ensure_ascii=False, separators=(",", ":")
            ),
            metrics=report["metrics_snapshot"],
            report_id=report["report_id"],
            report_path=str(output_path),
            report_sha256=report_sha,
        )
    return {"report": report, "report_path": str(output_path), "report_sha256": report_sha}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("review", help="review a Prediction handoff")
    command.add_argument("--handoff", required=True)
    command.add_argument("--output")
    command.add_argument("--min-cohort", type=int, default=3)
    command.add_argument("--low-diversity-similarity", type=float, default=0.80)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command != "review":
            raise AssertionError(args.command)
        result = run(
            handoff_path=args.handoff,
            output_path=args.output,
            config=CriticConfig(
                min_cohort_for_distribution=args.min_cohort,
                low_diversity_median_similarity=args.low_diversity_similarity,
            ),
        )
    except (CriticContractError, OSError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "complete",
        "report_path": result["report_path"],
        "report_sha256": result["report_sha256"],
        "report_id": result["report"]["report_id"],
        "verdict": result["report"]["verdict"],
        "issue_codes": [item["code"] for item in result["report"]["issues"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
