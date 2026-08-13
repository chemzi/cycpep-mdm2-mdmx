"""validation - split from agents/planner.py (PR6)."""

from __future__ import annotations

from collections.abc import Sequence

from contracts.exploration_decision import ExplorationDecision
from contracts.plan import validate_sha256
from prediction_pipeline.contracts import object_sha256
from .config import (
    MANDATORY_POLICY_CONSTRAINTS,
    PRIORITY_RANK,
    RECOMMENDATION_MAPPINGS,
    REPORT_ID_RE,
    SEVERITY_RANK,
)
from .errors import PlannerContractError


def _bind_exploration_decision(
    decision: ExplorationDecision,
    canonical: dict,
    *,
    report: dict,
    state: dict,
    project_id: str,
    workflow_id: str,
    source_round: int,
) -> dict[str, str]:
    """Validate and locally bind one already-valid E2 Decision."""
    source = report["source"]
    required_targets = source.get("required_targets")
    if (
        not isinstance(required_targets, Sequence)
        or isinstance(required_targets, (str, bytes))
        or not required_targets
        or any(
            not isinstance(target, str) or not target.strip()
            for target in required_targets
        )
        or len(set(required_targets)) != len(required_targets)
    ):
        raise PlannerContractError(
            "critic_required_targets_invalid",
            "Critic required_targets must be non-empty unique non-blank strings",
        )
    bindings = (
        (decision.project_id, project_id, "project"),
        (decision.workflow_id, workflow_id, "workflow"),
        (decision.source_round, source_round, "source round"),
        (decision.applies_to_round, source_round + 1, "applicable round"),
        (decision.prediction_run_id, source.get("prediction_run_id"), "Prediction run"),
        (sorted(decision.target_ids), sorted(required_targets), "target scope"),
    )
    for actual, expected, label in bindings:
        if actual != expected:
            raise PlannerContractError(
                "exploration_decision_binding_mismatch",
                f"ExplorationDecision {label} does not match Planner inputs",
            )
    state["_frozen_exploration_decision"] = canonical
    return {
        "decision_id": decision.decision_id,
        "decision_sha256": object_sha256(canonical),
        "decision_input_digest": decision.decision_input_digest,
    }


def _validate_critic_identity(report: dict) -> str:
    """Critic identity must be schema v1 and bound to its input digest."""
    if report.get("schema_version") != 1:
        raise PlannerContractError(
            "critic_schema_unsupported", "Planner requires Critic report schema v1"
        )
    report_id = str(report.get("report_id") or "")
    if not REPORT_ID_RE.fullmatch(report_id):
        raise PlannerContractError("critic_report_id_invalid", "invalid Critic report ID")
    input_digest = validate_sha256(
        report.get("input_digest"), "critic_input_digest_invalid", "Critic input_digest",
        error_cls=PlannerContractError,
    )
    if report_id != f"critic_{input_digest[:12]}":
        raise PlannerContractError(
            "critic_report_id_mismatch", "Critic report ID is not bound to input_digest"
        )
    verdict = report.get("verdict")
    if verdict not in {"blocked", "iterate", "review", "clear"}:
        raise PlannerContractError("critic_verdict_invalid", "unknown Critic verdict")
    if bool(report.get("passed")) != (verdict == "clear"):
        raise PlannerContractError(
            "critic_verdict_inconsistent", "Critic passed flag conflicts with verdict"
        )
    return report_id


def _validate_critic_source(
    report: dict, state: dict, report_sha256: str, report_id: str
) -> None:
    """Critic source and State must agree on project and report identity."""
    source = report.get("source")
    if not isinstance(source, dict):
        raise PlannerContractError("critic_source_invalid", "Critic source must be an object")
    project_id = str(source.get("project_id") or "").strip()
    state_project_id = str(state.get("project_id") or "").strip()
    if project_id and state_project_id and project_id != state_project_id:
        raise PlannerContractError(
            "planner_project_mismatch", "State and Critic report project IDs differ"
        )
    state_critic = state.get("critic") or {}
    if isinstance(state_critic, dict) and state_critic.get("report_id"):
        if state_critic["report_id"] != report_id:
            raise PlannerContractError(
                "state_critic_mismatch", "State points to a different Critic report"
            )
        declared_sha = str(state_critic.get("report_sha256") or "").strip().lower()
        if declared_sha and declared_sha != report_sha256:
            raise PlannerContractError(
                "state_critic_hash_mismatch", "State Critic report SHA-256 differs"
            )


def _validate_critic_issues(issues: list) -> dict[str, dict]:
    """Every Critic issue must be unique, severe, and safely actionable."""
    issues_by_code: dict[str, dict] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise PlannerContractError("critic_issue_invalid", "Critic issue must be an object")
        code = str(issue.get("code") or "").strip()
        action = str(issue.get("recommended_action") or "").strip()
        if not code or code in issues_by_code:
            raise PlannerContractError(
                "critic_issue_duplicate", f"missing or duplicate Critic issue code: {code!r}"
            )
        if issue.get("severity") not in SEVERITY_RANK:
            raise PlannerContractError(
                "critic_issue_severity_invalid", f"invalid severity for {code}"
            )
        if action not in RECOMMENDATION_MAPPINGS:
            raise PlannerContractError(
                "planner_action_unknown", f"Planner has no safe mapping for {action!r}"
            )
        candidate_ids = issue.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise PlannerContractError(
                "critic_issue_candidates_invalid", f"candidate_ids for {code} must be an array"
            )
        issues_by_code[code] = issue
    return issues_by_code


def _validate_critic_recommendations(
    recommendations: list, issues_by_code: dict[str, dict]
) -> list[str]:
    """Recommendations must be unique, mapped, and consistent with their issues."""
    recommendation_actions: list[str] = []
    covered_codes: set[str] = set()
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise PlannerContractError(
                "critic_recommendation_invalid", "Critic recommendation must be an object"
            )
        action = str(recommendation.get("action") or "").strip()
        if action not in RECOMMENDATION_MAPPINGS:
            raise PlannerContractError(
                "planner_action_unknown", f"Planner has no safe mapping for {action!r}"
            )
        if action in recommendation_actions:
            raise PlannerContractError(
                "critic_recommendation_duplicate", f"duplicate recommendation {action}"
            )
        if recommendation.get("priority") not in PRIORITY_RANK:
            raise PlannerContractError(
                "critic_priority_invalid", f"invalid priority for {action}"
            )
        reason_codes = recommendation.get("reason_codes")
        if not isinstance(reason_codes, list) or not reason_codes:
            raise PlannerContractError(
                "critic_reason_codes_invalid", f"recommendation {action} has no reasons"
            )
        for code in reason_codes:
            issue = issues_by_code.get(code)
            if issue is None or issue.get("recommended_action") != action:
                raise PlannerContractError(
                    "critic_recommendation_mismatch",
                    f"recommendation {action} is not supported by issue {code!r}",
                )
            covered_codes.add(code)
        recommendation_actions.append(action)
    if covered_codes != set(issues_by_code):
        raise PlannerContractError(
            "critic_recommendation_incomplete", "not every Critic issue is mapped"
        )
    return recommendation_actions


def _validate_critic_report(report: dict, state: dict, report_sha256: str) -> None:
    report_id = _validate_critic_identity(report)
    _validate_critic_source(report, state, report_sha256, report_id)
    issues = report.get("issues")
    recommendations = report.get("recommendations")
    handoff = report.get("planner_handoff")
    if not isinstance(issues, list) or not isinstance(recommendations, list):
        raise PlannerContractError(
            "critic_feedback_invalid", "Critic issues and recommendations must be arrays"
        )
    if not isinstance(handoff, dict):
        raise PlannerContractError(
            "critic_planner_handoff_invalid", "Critic planner_handoff must be an object"
        )
    if handoff.get("critic_report_id") != report_id:
        raise PlannerContractError(
            "critic_planner_handoff_mismatch", "planner_handoff report ID differs"
        )
    issues_by_code = _validate_critic_issues(issues)
    recommendation_actions = _validate_critic_recommendations(
        recommendations, issues_by_code
    )
    if handoff.get("issue_codes") != [issue["code"] for issue in issues]:
        raise PlannerContractError(
            "critic_handoff_issues_mismatch", "planner_handoff issue codes differ"
        )
    if handoff.get("recommended_actions") != recommendation_actions:
        raise PlannerContractError(
            "critic_handoff_actions_mismatch", "planner_handoff actions differ"
        )
    constraints = set(handoff.get("policy_constraints") or [])
    missing_constraints = sorted(MANDATORY_POLICY_CONSTRAINTS - constraints)
    if missing_constraints:
        raise PlannerContractError(
            "critic_policy_constraint_missing",
            f"Critic handoff lacks mandatory constraints: {missing_constraints}",
        )

