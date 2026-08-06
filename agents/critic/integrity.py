"""integrity - split from agents/critic.py (PR6)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from .config import ACTION_DEFAULTS, SEVERITY_RANK
from .config import CriticConfig

def _layer_has_missing_threshold(layer_key: str, missing_thresholds: list) -> bool:
    """Return whether a failed layer is unresolved because its gate is null."""
    prefix = f"{layer_key[:2].upper()}_"
    return any(str(key).upper().startswith(prefix) for key in missing_thresholds)

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
