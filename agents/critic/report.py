"""report - split from agents/critic.py (PR6)."""

from __future__ import annotations

from collections import Counter
from contracts.critic import critic_persistence_effects
from data_layer import CandidateIndex, EvidenceLogger, State
from dataclasses import asdict
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from .config import (
    CRITIC_VERSION,
    LAYER_KEYS,
    REPORT_SCHEMA_VERSION,
)
from .config import CriticConfig
from .diversity import _diversity_summary
from .errors import CriticContractError
from .integrity import _finalize_issues, _issue, _recommendations
from .io import _atomic_json
from .metrics import _target_metric_summary
from .record_review import _review_record
from .records import _load_records, _route_summary, _row_snapshot

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

    issues, layer_counts, unjustified_thresholds, diversity = (
        _collect_review_issues(records, rows_by_id, config)
    )
    statuses = Counter(item["status"] for item in records)
    verdict, passed = _derive_verdict(issues)
    issue_counts = dict(sorted(Counter(
        issue["severity"] for issue in issues
    ).items()))
    summary = (
        f"Critic reviewed {len(records)} candidate(s): verdict={verdict}; "
        f"statuses={dict(sorted(statuses.items()))}; issues={issue_counts}."
    )
    return _assemble_review_report(
        handoff=handoff,
        handoff_path=handoff_path,
        state=state,
        records=records,
        rows_by_id=rows_by_id,
        issues=issues,
        layer_counts=layer_counts,
        unjustified_thresholds=unjustified_thresholds,
        diversity=diversity,
        config=config,
        verdict=verdict,
        passed=passed,
        statuses=statuses,
        issue_counts=issue_counts,
        summary=summary,
    )


def _collect_review_issues(
    records: list[dict],
    rows_by_id: dict[str, dict],
    config: CriticConfig,
) -> tuple[dict[str, dict], dict[str, dict[str, int]], set[str], dict]:
    """Run per-record and cohort review; return final issue state."""
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
    finalized = _finalize_issues(issues, config)
    return finalized, layer_counts, unjustified_thresholds, diversity


def _assemble_review_report(
    *,
    handoff: dict,
    handoff_path: Path,
    state: dict,
    records: list[dict],
    rows_by_id: dict[str, dict],
    issues: dict[str, dict],
    layer_counts: dict[str, dict[str, int]],
    unjustified_thresholds: set[str],
    diversity: dict,
    config: CriticConfig,
    verdict: str,
    passed: bool,
    statuses: Counter,
    issue_counts: dict[str, int],
    summary: str,
) -> dict:
    """Assemble the immutable Critic report document."""
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
    recommendations = _recommendations(issues)
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
        "issues": issues,
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
            "issue_codes": [issue["code"] for issue in issues],
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
    state_updates, evidence_payload = critic_persistence_effects(
        report=report,
        report_path=output_path,
        report_digest=report_sha,
        state=state,
    )
    State.update(state_updates)
    State.append_history_if_absent(
        evidence_payload["history_entry"],
        identity_path=("summary", "report_id"),
        identity_value=report["report_id"],
    )
    if not any(
        entry.get("event_type") == "critic_review"
        and entry.get("report_id") == report["report_id"]
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.critic_review(**{
            key: value
            for key, value in evidence_payload.items()
            if key != "history_entry"
        })
    return {"report": report, "report_path": str(output_path), "report_sha256": report_sha}
