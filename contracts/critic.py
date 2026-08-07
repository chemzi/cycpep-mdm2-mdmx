"""Public Critic persistence effect contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def critic_persistence_effects(
    *,
    report: Mapping,
    report_path: str | Path | None = None,
    report_digest: str,
    state: Mapping,
    report_artifact_id: str | None = None,
) -> tuple[dict, dict]:
    """Build formal Critic state and evidence effects without publishing them.

    Transactional callers pass ``report_artifact_id`` (the committed artifact
    registry key) instead of a mutable task-local ``report_path``; the formal
    path is then resolved from the artifact registry at read time.  Legacy
    non-transactional callers may still pass ``report_path`` only.
    """
    if report_artifact_id is None and report_path is None:
        raise ValueError("report_artifact_id or report_path is required")
    summary = {
        "critic_version": report["critic_version"],
        "report_id": report["report_id"],
        "report_sha256": report_digest,
        "prediction_run_id": report["source"]["prediction_run_id"],
        "verdict": report["verdict"],
        "passed": report["passed"],
        "issue_counts": report["issue_counts"],
        "recommendation_count": len(report["recommendations"]),
    }
    if report_artifact_id is not None:
        summary["report_artifact_id"] = report_artifact_id
    if report_path is not None:
        summary["report_path"] = str(Path(report_path).expanduser().resolve())
    history_entry = {"phase": "critic", "agent": "critic", "summary": summary}
    evidence = {
        "issues": report["issues"],
        "passed": report["passed"],
        "summary": report["summary"],
        "recommendation": json.dumps(
            report["recommendations"], ensure_ascii=False, separators=(",", ":")
        ),
        "metrics": report["metrics_snapshot"],
        "report_id": report["report_id"],
        "report_sha256": report_digest,
        "history_entry": history_entry,
    }
    if report_artifact_id is not None:
        evidence["report_artifact_id"] = report_artifact_id
    if report_path is not None:
        evidence["report_path"] = summary["report_path"]
    return {
        "phase": "critic",
        "critic": summary,
    }, evidence


def resolve_critic_report_path(critic: Mapping, store) -> str | None:
    """Resolve the formal Critic report path from State.

    Prefers the committed artifact registry entry (``report_artifact_id``);
    falls back to a legacy inline ``report_path`` for pre-transactional rows.
    """
    artifact_id = critic.get("report_artifact_id")
    if artifact_id:
        artifact = store.get_artifact(str(artifact_id))
        if artifact is not None:
            return str(artifact["path"])
        return None
    report_path = critic.get("report_path")
    return str(report_path) if report_path else None
