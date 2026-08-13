"""Plan contract shared by Planner and Orchestrator.

The plan schema, mandatory policy constraints, and approval-time revalidation
live here so Orchestrator never depends on Planner implementation details.
Callers may inject their own error class via ``error_cls`` so each agent keeps
its own domain error type.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .action import get_action_spec
from .trace import TraceContext, derive_workflow_id
from prediction_pipeline.contracts import file_sha256, object_sha256


PLANNER_VERSION = "1.2.1"
PLAN_SCHEMA_VERSION = 2
LEGACY_PLAN_SCHEMA_VERSION = 1

MANDATORY_POLICY_CONSTRAINTS = frozenset({
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
})


class PlanContractError(ValueError):
    """A plan or approval artifact is unsafe to use across agent boundaries."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_approval_gpu_minutes(
    plan: dict,
    selected_task_ids: list[str],
    max_gpu_minutes: float | None,
    *,
    error_cls: type = PlanContractError,
) -> float:
    """Return selected GPU-task estimates after enforcing approval admission."""
    tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    gpu_tasks = [
        tasks_by_id[task_id]
        for task_id in selected_task_ids
        if tasks_by_id[task_id]["resource_request"]["class"] == "gpu"
    ]
    if not gpu_tasks:
        return 0.0

    estimates = []
    for task in gpu_tasks:
        resource = task["resource_request"]
        estimate = resource.get("estimated_gpu_minutes")
        if (
            resource.get("estimate_status") != "estimated"
            or not isinstance(estimate, (int, float))
            or isinstance(estimate, bool)
            or not math.isfinite(float(estimate))
            or estimate <= 0
        ):
            raise error_cls(
                "approval_gpu_estimate_invalid",
                f"selected GPU task {task['task_id']} lacks a usable estimated_gpu_minutes",
            )
        estimates.append(float(estimate))

    if (
        not isinstance(max_gpu_minutes, (int, float))
        or isinstance(max_gpu_minutes, bool)
        or not math.isfinite(float(max_gpu_minutes))
        or max_gpu_minutes <= 0
    ):
        raise error_cls(
            "approval_gpu_minutes_invalid",
            "max_gpu_minutes must be a positive finite number for selected GPU tasks",
        )
    required_minutes = math.fsum(estimates)
    if float(max_gpu_minutes) < required_minutes:
        raise error_cls(
            "approval_gpu_minutes_insufficient",
            "max_gpu_minutes is lower than the selected GPU-task estimates "
            f"({float(max_gpu_minutes):g} < {required_minutes:g})",
        )
    return required_minutes


def validate_sha256(
    value: Any, code: str, label: str, *, error_cls: type = PlanContractError
) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise error_cls(code, f"{label} must be a full SHA-256")
    return digest


def _canonicalize_plan(plan: dict, *, error_cls: type = PlanContractError) -> dict:
    """Adapt a legacy v1 plan in memory to the strict v2 trace contract."""
    if not isinstance(plan, dict):
        raise error_cls("plan_invalid", "plan must be an object")
    version = plan.get("schema_version")
    if version == LEGACY_PLAN_SCHEMA_VERSION:
        canonical = json.loads(json.dumps(plan))
        source = canonical.get("source")
        if not isinstance(source, dict):
            raise error_cls("plan_source_invalid", "plan source must be an object")
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
        raise error_cls("plan_schema_unsupported", "unsupported plan schema")
    return plan


def _validate_plan_policy(plan: dict, *, error_cls: type = PlanContractError) -> None:
    """The plan must be current and prohibit automatic dispatch."""
    if plan.get("planner_version") != PLANNER_VERSION:
        raise error_cls(
            "plan_version_unsupported", "approval requires the current Planner version"
        )
    constraints = set(plan.get("policy_constraints") or [])
    missing = sorted(MANDATORY_POLICY_CONSTRAINTS - constraints)
    if missing:
        raise error_cls(
            "plan_policy_constraint_missing",
            f"plan lacks mandatory constraints: {missing}",
        )
    execution = plan.get("execution")
    if not isinstance(execution, dict) or execution.get("automatic_dispatch_allowed") is not False:
        raise error_cls(
            "plan_execution_policy_invalid", "plan must prohibit automatic dispatch"
        )
    if execution.get("orchestrator_required") is not True:
        raise error_cls(
            "plan_execution_policy_invalid", "plan must require Orchestrator validation"
        )


def _validate_plan_source(
    plan: dict, plan_path: Path, *, error_cls: type = PlanContractError
) -> None:
    """The plan must bind to one explicit immutable source and stable workflow."""
    source = plan.get("source")
    if not isinstance(source, dict):
        raise error_cls("plan_source_invalid", "plan source must be an object")
    workflow_id = plan.get("workflow_id")
    source_workflow_id = source.get("workflow_id")
    if not workflow_id or not source_workflow_id:
        raise error_cls(
            "plan_workflow_id_missing", "canonical plan requires workflow_id at plan and source"
        )
    if workflow_id != source_workflow_id:
        raise error_cls(
            "plan_workflow_id_mismatch", "plan and source workflow_id differ"
        )
    try:
        TraceContext(
            project_id=str(source.get("project_id") or "unknown_project"),
            workflow_id=str(workflow_id),
            plan_id=str(plan.get("plan_id") or "") or None,
        )
    except ValueError as exc:
        raise error_cls("plan_workflow_id_invalid", "plan workflow_id is invalid") from exc
    if source.get("kind") == "initial_prediction_bootstrap":
        _validate_bootstrap_source(source, error_cls=error_cls)
        return
    critic_path_value = str(source.get("critic_report") or "").strip()
    if not critic_path_value:
        raise error_cls("plan_source_invalid", "plan has no Critic report path")
    critic_path = Path(critic_path_value).expanduser()
    if not critic_path.is_absolute():
        critic_path = (plan_path.parent / critic_path).resolve()
    else:
        critic_path = critic_path.resolve()
    declared_critic_sha = validate_sha256(
        source.get("critic_report_sha256"),
        "plan_source_hash_invalid",
        "plan Critic report SHA-256",
        error_cls=error_cls,
    )
    if not critic_path.is_file():
        raise error_cls("plan_source_missing", f"Critic report missing: {critic_path}")
    if file_sha256(critic_path) != declared_critic_sha:
        raise error_cls(
            "plan_source_hash_mismatch", "Critic report changed after planning"
        )


def _validate_bootstrap_source(source: dict, *, error_cls: type) -> None:
    allowed = {
        "kind", "project_id", "approved_content_binding", "launcher_run_id",
        "research_completion_event_id", "design_invocation_id",
        "design_completion_event_id", "design_transaction_id", "candidate_ids",
        "execution_identity", "workflow_id", "retry",
    }
    unknown = sorted(set(source) - allowed)
    if unknown:
        raise error_cls(
            "bootstrap_source_invalid", f"bootstrap source has unsupported fields: {unknown}"
        )
    for key in (
        "project_id", "approved_content_binding", "launcher_run_id",
        "research_completion_event_id", "design_invocation_id",
        "design_completion_event_id", "design_transaction_id",
    ):
        if not isinstance(source.get(key), str) or not source[key]:
            raise error_cls("bootstrap_source_invalid", f"bootstrap source requires {key}")
    candidates = source.get("candidate_ids")
    if (
        not isinstance(candidates, list) or not candidates
        or candidates != sorted(set(candidates))
        or any(not isinstance(value, str) or not value for value in candidates)
    ):
        raise error_cls(
            "bootstrap_candidate_scope_invalid",
            "bootstrap source candidate_ids must be the sorted exact committed set",
        )
    try:
        from prediction_pipeline.execution_identity import validate_prediction_execution_identity
        validate_prediction_execution_identity(source.get("execution_identity"))
    except (TypeError, ValueError) as exc:
        raise error_cls(getattr(exc, "code", "bootstrap_identity_invalid"), str(exc)) from exc
    retry = source.get("retry")
    if retry is not None:
        required_retry = {
            "retry_index", "prior_plan_id", "prior_run_id", "prior_task_id",
            "prior_attempt_id", "prior_transaction_id", "failure_event_id",
            "failure_status",
        }
        if (
            not isinstance(retry, dict)
            or set(retry) != required_retry
            or not isinstance(retry.get("retry_index"), int)
            or isinstance(retry.get("retry_index"), bool)
            or retry["retry_index"] < 1
            or retry.get("failure_status") != "failed"
        ):
            raise error_cls("bootstrap_retry_invalid", "bootstrap retry binding is invalid")


def _validate_plan_tasks(
    plan: dict, *, error_cls: type = PlanContractError
) -> dict[str, dict]:
    """Every task must carry a valid contract and executable action."""
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        raise error_cls("plan_tasks_invalid", "plan tasks must be an array")
    tasks_by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise error_cls("plan_task_invalid", "every plan task must be an object")
        task_id = str(task.get("task_id") or "")
        if not re.fullmatch(r"T[0-9]{3}", task_id) or task_id in tasks_by_id:
            raise error_cls(
                "plan_task_id_invalid", f"missing, invalid, or duplicate task ID: {task_id!r}"
            )
        resource = task.get("resource_request")
        approval = task.get("approval")
        if not isinstance(resource, dict) or not isinstance(approval, dict):
            raise error_cls(
                "plan_task_contract_invalid", f"task {task_id} lacks resource/approval contract"
            )
        if resource.get("class") == "gpu" and (
            approval.get("required") is not True
            or "execution_budget" not in (approval.get("types") or [])
        ):
            raise error_cls(
                "plan_gpu_approval_missing",
                f"GPU task {task_id} lacks execution-budget approval",
            )
        gate = task.get("execution_gate")
        if not isinstance(gate, dict) or gate.get("status") not in {"proposed", "blocked"}:
            raise error_cls(
                "plan_task_gate_invalid", f"task {task_id} has an invalid execution gate"
            )
        try:
            from execution.contracts import ALL_KNOWN_ACTIONS, validate_task_parameters

            action = str(task.get("action") or "")
            if action not in ALL_KNOWN_ACTIONS:
                raise error_cls(
                    "plan_action_unknown", f"task {task_id} has unknown action {action!r}"
                )
            spec = get_action_spec(action)
            if not spec.executable and gate.get("status") == "proposed":
                raise error_cls(
                    "plan_unimplemented_action_proposed",
                    f"task {task_id} uses non-executable action {action}",
                )
            if spec.executable and gate.get("status") == "proposed":
                validate_task_parameters(task)
        except error_cls:
            raise
        except Exception as exc:
            raise error_cls(
                getattr(exc, "code", "plan_execution_contract_invalid"), str(exc)
            ) from exc
        tasks_by_id[task_id] = task
    return tasks_by_id


def _validate_plan_dependencies(
    tasks_by_id: dict[str, dict], *, error_cls: type = PlanContractError
) -> None:
    """Task dependencies must resolve without unknown or cyclic edges."""
    for task_id, task in tasks_by_id.items():
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or task_id in dependencies:
            raise error_cls(
                "plan_dependency_invalid", f"task {task_id} has invalid dependencies"
            )
        unknown = sorted(set(dependencies) - set(tasks_by_id))
        if unknown:
            raise error_cls(
                "plan_dependency_unknown", f"task {task_id} depends on {unknown}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise error_cls(
                "plan_dependency_cycle", "plan task dependencies contain a cycle"
            )
        visiting.add(task_id)
        for dependency in tasks_by_id[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks_by_id:
        visit(task_id)


def validate_plan_for_approval(
    plan: dict, plan_path: Path, *, error_cls: type = PlanContractError
) -> dict:
    """Recheck security invariants before an approval artifact can be issued."""
    plan = _canonicalize_plan(plan, error_cls=error_cls)
    _validate_plan_policy(plan, error_cls=error_cls)
    _validate_plan_source(plan, plan_path, error_cls=error_cls)
    tasks_by_id = _validate_plan_tasks(plan, error_cls=error_cls)
    _validate_plan_dependencies(tasks_by_id, error_cls=error_cls)
    if (plan.get("source") or {}).get("kind") == "initial_prediction_bootstrap":
        _validate_bootstrap_plan_task(plan, tasks_by_id, error_cls=error_cls)
    return plan


def _validate_bootstrap_plan_task(
    plan: dict, tasks_by_id: dict[str, dict], *, error_cls: type
) -> None:
    if len(tasks_by_id) != 1:
        raise error_cls(
            "bootstrap_tasks_invalid", "bootstrap plan must contain exactly one task"
        )
    task = next(iter(tasks_by_id.values()))
    source = plan["source"]
    if (
        task.get("action") != "evaluate_new_design_candidates"
        or task.get("agent") != "prediction"
        or task.get("candidate_scope") != {
            "candidate_ids": source["candidate_ids"], "from_task_id": None
        }
        or (task.get("resource_request") or {}).get("candidate_limit")
        != len(source["candidate_ids"])
        or (task.get("parameters") or {}).get("execution_identity")
        != source["execution_identity"]
    ):
        raise error_cls(
            "bootstrap_task_binding_invalid",
            "bootstrap task differs from its exact Design scope or execution identity",
        )

