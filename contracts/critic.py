"""Public Critic persistence effect contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def critic_persistence_effects(
    *,
    report: Mapping,
    report_path: str | Path,
    report_digest: str,
    state: Mapping,
) -> tuple[dict, dict]:
    """Build formal Critic state and evidence effects without publishing them."""
    summary = {
        "critic_version": report["critic_version"],
        "report_id": report["report_id"],
        "report_path": str(Path(report_path).expanduser().resolve()),
        "report_sha256": report_digest,
        "prediction_run_id": report["source"]["prediction_run_id"],
        "verdict": report["verdict"],
        "passed": report["passed"],
        "issue_counts": report["issue_counts"],
        "recommendation_count": len(report["recommendations"]),
    }
    history = list(state.get("iteration_history") or [])
    if not any(
        item.get("agent") == "critic"
        and (item.get("summary") or {}).get("report_id") == report["report_id"]
        for item in history
    ):
        history.append({"phase": "critic", "agent": "critic", "summary": summary})
    evidence = {
        "issues": report["issues"],
        "passed": report["passed"],
        "summary": report["summary"],
        "recommendation": json.dumps(
            report["recommendations"], ensure_ascii=False, separators=(",", ":")
        ),
        "metrics": report["metrics_snapshot"],
        "report_id": report["report_id"],
        "report_path": summary["report_path"],
        "report_sha256": report_digest,
    }
    return {
        "phase": "critic",
        "critic": summary,
        "iteration_history": history,
    }, evidence
