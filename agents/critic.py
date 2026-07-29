"""
Critic Agent — 赵嘉策
职责：每轮结束后检查全局候选池质量，产出评审报告，触发 Planner 策略调整
入口：review(candidates, thresholds=None, state=None) → dict
依赖：from data_layer import State, EvidenceLogger, CandidateIndex, evaluate_battery
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from data_layer import (
    CandidateIndex,
    EvidenceLogger,
    State,
    evaluate_battery,
)


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _is_scored(row: dict) -> bool:
    for key in ("plddt", "ipsae_mdm2", "l1_pass", "all_layers_pass", "metric_clearance"):
        if row.get(key) not in (None, ""):
            return True
    return False


def _threshold_pending(thresholds: dict) -> list[dict]:
    """Return issues for thresholds that lack calibration / evidence grade."""
    issues = []
    if not thresholds:
        return [{
            "code": "threshold_uncalibrated",
            "message": "No thresholds present in state; calibration_status pending.",
            "owner_hint": "prediction/research",
        }]
    pending_keys = []
    for key, entry in thresholds.items():
        if not isinstance(entry, dict):
            pending_keys.append(key)
            continue
        calibration = str(entry.get("calibration_status") or "").casefold()
        grade = str(entry.get("evidence_grade") or entry.get("grade") or "").casefold()
        justified = calibration in {"calibrated", "validated", "complete"} or grade in {
            "paper_explicit", "method_explicit", "design_rule",
            "field_consensus", "positive_control", "empirical_null",
        }
        if not justified:
            pending_keys.append(key)
    if pending_keys:
        issues.append({
            "code": "threshold_needs_review",
            "message": (
                f"{len(pending_keys)} threshold(s) lack calibration/evidence grade: "
                + ", ".join(pending_keys[:8])
                + ("..." if len(pending_keys) > 8 else "")
            ),
            "owner_hint": "prediction/research",
            "keys": pending_keys,
        })
    return issues


def review(
    candidates: Optional[list] = None,
    thresholds: Optional[dict] = None,
    state: Optional[dict] = None,
    log_evidence: bool = True,
    round_num: Optional[int] = None,
) -> dict:
    """
    Engineering-first critic review.

    Checks:
      - empty candidate pool
      - duplicate sequences
      - missing manifest_path
      - unscored candidates
      - uncalibrated thresholds

    Verdict uses metric_clearance (not competition_clearance), because
    competition_clearance stays False while thresholds are pending.
    """
    state = state if state is not None else State.load()
    if thresholds is None:
        thresholds = state.get("thresholds") or {}
    if candidates is None:
        candidates = CandidateIndex.load()

    issues: list[dict] = []

    # 1. Empty pool
    if not candidates:
        issues.append({
            "code": "empty_candidate_pool",
            "message": "No candidates found in CandidateIndex.",
            "owner_hint": "design",
        })

    # 2. Duplicate sequences
    seqs = [str(c.get("sequence") or "") for c in candidates if c.get("sequence")]
    counts = Counter(seqs)
    dup_seqs = [s for s, n in counts.items() if s and n > 1]
    if dup_seqs:
        n_dup_rows = sum(n for s, n in counts.items() if s and n > 1)
        uniq = len(set(seqs))
        total = len(seqs) or 1
        uniqueness = uniq / total
        issues.append({
            "code": "duplicate_sequences",
            "message": (
                f"{len(dup_seqs)} sequence(s) repeated; "
                f"uniqueness={uniqueness:.2f} ({uniq}/{total})."
            ),
            "owner_hint": "design",
            "duplicate_count": len(dup_seqs),
            "uniqueness": round(uniqueness, 3),
        })
        if uniqueness < 0.5:
            issues.append({
                "code": "low_diversity",
                "message": f"Sequence uniqueness {uniqueness:.2f} < 0.5.",
                "owner_hint": "design/planner",
            })

    # 3. Missing manifest
    missing_manifest = [
        c.get("candidate_id") for c in candidates
        if c.get("candidate_id") and not str(c.get("manifest_path") or "").strip()
    ]
    if missing_manifest:
        issues.append({
            "code": "missing_manifest",
            "message": f"{len(missing_manifest)} candidate(s) missing manifest_path.",
            "owner_hint": "design",
            "candidate_ids": missing_manifest[:20],
        })

    # 4. Unscored
    unscored = [
        c.get("candidate_id") for c in candidates
        if c.get("candidate_id") and not _is_scored(c)
    ]
    if candidates and unscored:
        issues.append({
            "code": "unscored_candidates",
            "message": f"{len(unscored)} candidate(s) have no scores yet.",
            "owner_hint": "prediction",
            "candidate_ids": unscored[:20],
        })

    # 5. Threshold calibration
    issues.extend(_threshold_pending(thresholds))

    # Metric clearance on scored candidates (engineering gate for "done")
    clearance_pass = 0
    clearance_fail = 0
    scored_rows = [c for c in candidates if _is_scored(c)]
    for row in scored_rows:
        try:
            result = evaluate_battery(row, thresholds=thresholds)
        except Exception as exc:
            issues.append({
                "code": "battery_eval_error",
                "message": f"evaluate_battery failed for {row.get('candidate_id')}: {exc}",
                "owner_hint": "prediction",
            })
            clearance_fail += 1
            continue
        if result.get("metric_clearance"):
            clearance_pass += 1
        else:
            clearance_fail += 1

    # Verdict
    hard_codes = {
        "empty_candidate_pool",
        "unscored_candidates",
        "missing_manifest",
    }
    advisory_codes = {
        "threshold_needs_review",
        "threshold_uncalibrated",
    }
    hard_issues = [i for i in issues if i.get("code") in hard_codes]
    advisory_issues = [i for i in issues if i.get("code") in advisory_codes]

    if not candidates:
        verdict = "dead_end"
        recommendation = "return to design; candidate pool is empty"
    elif hard_issues:
        # Missing scores / manifests → backtrack to fix upstream, not declare done
        if any(i["code"] == "unscored_candidates" for i in hard_issues):
            verdict = "backtrack"
            recommendation = "score remaining candidates before iterating strategy"
        elif any(i["code"] == "missing_manifest" for i in hard_issues):
            verdict = "backtrack"
            recommendation = "reject incomplete designs; regenerate with manifests"
        else:
            verdict = "dead_end"
            recommendation = "hard engineering failure; backtrack and redesign"
    elif scored_rows and clearance_pass > 0 and not hard_issues:
        # Funnel reality: failed candidates remain in the pool as negative evidence.
        # "done" means at least one metric_clearance hit; pending thresholds stay advisory.
        if clearance_fail == 0 or clearance_pass >= max(1, (len(scored_rows) + 2) // 3):
            verdict = "done"
            recommendation = (
                "metric_clearance achieved; ready to report"
                + ("; thresholds still need calibration" if advisory_issues else "")
            )
        else:
            verdict = "advance"
            recommendation = "partial clearance; deepen search with refined strategy"
    elif clearance_pass > 0 and not hard_issues:
        verdict = "advance"
        recommendation = "partial clearance; deepen search with refined strategy"
    else:
        verdict = "backtrack"
        recommendation = "no metric_clearance; try an untried sibling strategy"

    passed = verdict in ("done", "advance") and not hard_issues
    summary = (
        f"verdict={verdict}; candidates={len(candidates)}; "
        f"scored={len(scored_rows)}; metric_clearance_pass={clearance_pass}; "
        f"issues={len(issues)}"
    )
    metrics = {
        "total_candidates": len(candidates),
        "scored": len(scored_rows),
        "metric_clearance_pass": clearance_pass,
        "metric_clearance_fail": clearance_fail,
        "issue_count": len(issues),
        "hard_issue_count": len(hard_issues),
        "advisory_issue_count": len(advisory_issues),
    }

    event_id = None
    if log_evidence:
        event_id = EvidenceLogger.critic_review(
            issues=issues,
            passed=passed,
            summary=summary,
            recommendation=recommendation,
            metrics=metrics,
            round_num=round_num if round_num is not None else state.get("round"),
        )

    return {
        "status": "ok" if passed else "needs_attention",
        "verdict": verdict,
        "passed": passed,
        "issues": issues,
        "summary": summary,
        "recommendation": recommendation,
        "metrics": metrics,
        "event_id": event_id,
    }
