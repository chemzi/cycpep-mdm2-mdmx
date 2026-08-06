"""validation - split from agents/planner.py (PR6)."""

from __future__ import annotations

import json, re
from contracts.action import get_action_spec
from contracts.trace import TraceContext, derive_workflow_id
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from typing import Any
from .errors import PlannerContractError

from .config import (
    LEGACY_PLAN_SCHEMA_VERSION,
    MANDATORY_POLICY_CONSTRAINTS,
    PLANNER_VERSION,
    PLAN_SCHEMA_VERSION,
    PRIORITY_RANK,
    RECOMMENDATION_MAPPINGS,
    REPORT_ID_RE,
    SEVERITY_RANK,
)

def _validate_sha256(value: Any, code: str, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PlannerContractError(code, f"{label} must be a full SHA-256")
    return digest

def _validate_critic_report(report: dict, state: dict, report_sha256: str) -> None:
    if report.get("schema_version") != 1:
        raise PlannerContractError(
            "critic_schema_unsupported", "Planner requires Critic report schema v1"
        )
    report_id = str(report.get("report_id") or "")
    if not REPORT_ID_RE.fullmatch(report_id):
        raise PlannerContractError("critic_report_id_invalid", "invalid Critic report ID")
    input_digest = _validate_sha256(
        report.get("input_digest"), "critic_input_digest_invalid", "Critic input_digest"
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

def _canonicalize_plan(plan: dict) -> dict:
    """Adapt a legacy v1 plan in memory to the strict v2 trace contract."""
    if not isinstance(plan, dict):
        raise PlannerContractError("plan_invalid", "plan must be an object")
    version = plan.get("schema_version")
    if version == LEGACY_PLAN_SCHEMA_VERSION:
        canonical = json.loads(json.dumps(plan))
        source = canonical.get("source")
        if not isinstance(source, dict):
            raise PlannerContractError("plan_source_invalid", "plan source must be an object")
        project_id = str(source.get("project_id") or "unknown_project")
        workflow_id = canonical.get("workflow_id") or source.get("workflow_id")
        if not workflow_id:
            source_id = str(source.get("critic_report_id") or canonical.get("plan_id") or "plan")
            source_sha256 = str(
                source.get("critic_report_sha256") or canonical.get("input_digest") or ""
            )
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                source_sha256 = object_sha256(plan)
            workflow_id = derive_workflow_id(
                project_id,
                source_id,
                source_sha256,
                (canonical.get("cycle") or {}).get("source_round", 1),
            )
        canonical["workflow_id"] = str(workflow_id)
        source["workflow_id"] = str(workflow_id)
        canonical["schema_version"] = PLAN_SCHEMA_VERSION
        return canonical
    if version != PLAN_SCHEMA_VERSION:
        raise PlannerContractError("plan_schema_unsupported", "unsupported plan schema")
    return plan

def _validate_plan_for_approval(plan: dict, plan_path: Path) -> dict:
    """Recheck security invariants before an approval artifact can be issued."""
    plan = _canonicalize_plan(plan)
    if plan.get("planner_version") != PLANNER_VERSION:
        raise PlannerContractError(
            "plan_version_unsupported", "approval requires the current Planner version"
        )
    constraints = set(plan.get("policy_constraints") or [])
    missing = sorted(MANDATORY_POLICY_CONSTRAINTS - constraints)
    if missing:
        raise PlannerContractError(
            "plan_policy_constraint_missing",
            f"plan lacks mandatory constraints: {missing}",
        )
    execution = plan.get("execution")
    if not isinstance(execution, dict) or execution.get("automatic_dispatch_allowed") is not False:
        raise PlannerContractError(
            "plan_execution_policy_invalid", "plan must prohibit automatic dispatch"
        )
    if execution.get("orchestrator_required") is not True:
        raise PlannerContractError(
            "plan_execution_policy_invalid", "plan must require Orchestrator validation"
        )

    source = plan.get("source")
    if not isinstance(source, dict):
        raise PlannerContractError("plan_source_invalid", "plan source must be an object")
    workflow_id = plan.get("workflow_id")
    source_workflow_id = source.get("workflow_id")
    if not workflow_id or not source_workflow_id:
        raise PlannerContractError(
            "plan_workflow_id_missing", "canonical plan requires workflow_id at plan and source"
        )
    if workflow_id != source_workflow_id:
        raise PlannerContractError(
            "plan_workflow_id_mismatch", "plan and source workflow_id differ"
        )
    try:
        TraceContext(
            project_id=str(source.get("project_id") or "unknown_project"),
            workflow_id=str(workflow_id),
            plan_id=str(plan.get("plan_id") or "") or None,
        )
    except ValueError as exc:
        raise PlannerContractError("plan_workflow_id_invalid", "plan workflow_id is invalid") from exc
    critic_path_value = str(source.get("critic_report") or "").strip()
    if not critic_path_value:
        raise PlannerContractError("plan_source_invalid", "plan has no Critic report path")
    critic_path = Path(critic_path_value).expanduser()
    if not critic_path.is_absolute():
        critic_path = (plan_path.parent / critic_path).resolve()
    else:
        critic_path = critic_path.resolve()
    declared_critic_sha = _validate_sha256(
        source.get("critic_report_sha256"),
        "plan_source_hash_invalid",
        "plan Critic report SHA-256",
    )
    if file_sha256(critic_path) != declared_critic_sha:
        raise PlannerContractError(
            "plan_source_hash_mismatch", "Critic report changed after planning"
        )

    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise PlannerContractError("plan_tasks_invalid", "plan tasks must be an array")
    tasks_by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise PlannerContractError("plan_task_invalid", "every plan task must be an object")
        task_id = str(task.get("task_id") or "")
        if not re.fullmatch(r"T[0-9]{3}", task_id) or task_id in tasks_by_id:
            raise PlannerContractError(
                "plan_task_id_invalid", f"missing, invalid, or duplicate task ID: {task_id!r}"
            )
        resource = task.get("resource_request")
        approval = task.get("approval")
        if not isinstance(resource, dict) or not isinstance(approval, dict):
            raise PlannerContractError(
                "plan_task_contract_invalid", f"task {task_id} lacks resource/approval contract"
            )
        if resource.get("class") == "gpu" and (
            approval.get("required") is not True
            or "execution_budget" not in (approval.get("types") or [])
        ):
            raise PlannerContractError(
                "plan_gpu_approval_missing",
                f"GPU task {task_id} lacks execution-budget approval",
            )
        gate = task.get("execution_gate")
        if not isinstance(gate, dict) or gate.get("status") not in {"proposed", "blocked"}:
            raise PlannerContractError(
                "plan_task_gate_invalid", f"task {task_id} has an invalid execution gate"
            )
        try:
            from execution.contracts import ALL_KNOWN_ACTIONS, validate_task_parameters
            from contracts.action import get_action_spec

            action = str(task.get("action") or "")
            if action not in ALL_KNOWN_ACTIONS:
                raise PlannerContractError(
                    "plan_action_unknown", f"task {task_id} has unknown action {action!r}"
                )
            spec = get_action_spec(action)
            if not spec.executable and gate.get("status") == "proposed":
                raise PlannerContractError(
                    "plan_unimplemented_action_proposed",
                    f"task {task_id} uses non-executable action {action}",
                )
            if spec.executable and gate.get("status") == "proposed":
                validate_task_parameters(task)
        except PlannerContractError:
            raise
        except Exception as exc:
            raise PlannerContractError(
                getattr(exc, "code", "plan_execution_contract_invalid"), str(exc)
            ) from exc
        tasks_by_id[task_id] = task
    for task_id, task in tasks_by_id.items():
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or task_id in dependencies:
            raise PlannerContractError(
                "plan_dependency_invalid", f"task {task_id} has invalid dependencies"
            )
        unknown = sorted(set(dependencies) - set(tasks_by_id))
        if unknown:
            raise PlannerContractError(
                "plan_dependency_unknown", f"task {task_id} depends on {unknown}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise PlannerContractError(
                "plan_dependency_cycle", "plan task dependencies contain a cycle"
            )
        visiting.add(task_id)
        for dependency in tasks_by_id[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks_by_id:
        visit(task_id)
    return plan
