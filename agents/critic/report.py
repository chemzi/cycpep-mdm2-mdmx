"""report - split from agents/critic.py (PR6)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from data_layer import CandidateIndex, EvidenceLogger, State
from dataclasses import asdict
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from .config import (
    ALLOWED_STATUSES,
    CRITIC_VERSION,
    LAYER_ISSUES,
    LAYER_KEYS,
    REPORT_SCHEMA_VERSION,
)
from .config import CriticConfig
from .diversity import _diversity_summary
from .errors import CriticContractError
from .integrity import (
    _finalize_issues,
    _issue,
    _layer_has_missing_threshold,
    _recommendations,
)
from .io import _atomic_json, _json_object, _resolve_path
from .metrics import _metric_evidence, _target_metric_summary

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

def _inject_project_config(state: dict, project_config: dict | None) -> None:
    """Bind an explicitly approved project config into the review inputs."""
    if project_config is None:
        return
    state["project_config"] = project_config
    injected_project_id = str(project_config.get("project_id") or "").strip()
    existing_project_id = str(state.get("project_id") or "").strip()
    if (
        injected_project_id
        and existing_project_id
        and injected_project_id != existing_project_id
    ):
        raise CriticContractError(
            "critic_project_mismatch",
            "injected project config differs from State project ID",
        )
    if injected_project_id:
        state["project_id"] = injected_project_id


def _validate_handoff_project(state: dict, handoff: dict) -> None:
    """State and handoff project IDs must agree when both are present."""
    if state.get("project_id") and handoff.get("project_id") and (
        state["project_id"] != handoff["project_id"]
    ):
        raise CriticContractError(
            "critic_project_mismatch", "State and Prediction handoff project IDs differ"
        )


def _index_candidate_rows(candidate_rows: list[dict]) -> dict[str, dict]:
    """Index CandidateIndex rows by candidate ID, rejecting duplicates."""
    rows_by_id: dict[str, dict] = {}
    for row in candidate_rows:
        candidate_id = str(row.get("candidate_id") or "").strip()
        if candidate_id in rows_by_id:
            raise CriticContractError(
                "candidate_index_duplicate", f"duplicate CandidateIndex row {candidate_id}"
            )
        if candidate_id:
            rows_by_id[candidate_id] = row
    return rows_by_id


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
        return
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


def _cohort_review_issues(
    records: list[dict],
    issues: dict[str, dict],
    config: CriticConfig,
    diversity: dict,
    unjustified_thresholds: set[str],
    unjustified_candidates: set[str],
) -> None:
    """Emit calibration, duplication, and cohort-level review issues."""
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


def _derive_verdict(finalized_issues: list[dict]) -> tuple[str, bool]:
    """Map finalized issue severities onto the Critic verdict."""
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
    return verdict, verdict == "clear"


def _row_snapshot(records: list[dict], rows_by_id: dict[str, dict]) -> list[dict]:
    """Snapshot the CandidateIndex row each record was reviewed against."""
    return [
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


def review(
    *,
    handoff_path: str | Path,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    config: CriticConfig | None = None,
    project_config: dict | None = None,
) -> dict:
    """Purely review one immutable Prediction handoff and its records.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7); it must agree with ``state``'s
    ``project_id`` when present, and supplies the project identity when State
    has none.  When omitted, behaviour is unchanged.
    """
    config = config or CriticConfig()
    handoff_path = Path(handoff_path).expanduser().resolve()
    handoff, records = _load_records(handoff_path)
    state = dict(state if state is not None else State.load())
    _inject_project_config(state, project_config)
    _validate_handoff_project(state, handoff)
    rows_by_id = _index_candidate_rows(list(
        candidate_rows if candidate_rows is not None else CandidateIndex.load()
    ))

    issues: dict[str, dict] = {}
    layer_counts = {key: {"pass": 0, "fail": 0, "missing": 0} for key in LAYER_KEYS}
    unjustified_thresholds: set[str] = set()
    unjustified_candidates: set[str] = set()
    for item in records:
        _review_record(
            item,
            rows_by_id,
            issues,
            layer_counts,
            unjustified_thresholds,
            unjustified_candidates,
        )

    diversity = _diversity_summary(records)
    _cohort_review_issues(
        records,
        issues,
        config,
        diversity,
        unjustified_thresholds,
        unjustified_candidates,
    )
    finalized_issues = _finalize_issues(issues, config)
    statuses = Counter(item["status"] for item in records)
    verdict, passed = _derive_verdict(finalized_issues)
    issue_counts = dict(sorted(Counter(
        issue["severity"] for issue in finalized_issues
    ).items()))
    summary = (
        f"Critic reviewed {len(records)} candidate(s): verdict={verdict}; "
        f"statuses={dict(sorted(statuses.items()))}; issues={issue_counts}."
    )

    row_snapshot = _row_snapshot(records, rows_by_id)
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
def run(
    *,
    handoff_path: str | Path,
    output_path: str | Path | None = None,
    state: dict | None = None,
    candidate_rows: list[dict] | None = None,
    config: CriticConfig | None = None,
    project_config: dict | None = None,
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
        project_config=project_config,
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
