"""plan_builder - split from agents/planner/service.py (PR6)."""

from __future__ import annotations

from collections.abc import Mapping
from contracts.exploration_decision import ExplorationDecision
from contracts.trace import TraceContext, derive_workflow_id
from data_layer import State
from dataclasses import asdict
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from .budget import _budget_snapshot
from .config import (
    APPROVAL_SCHEMA_VERSION,
    MANDATORY_POLICY_CONSTRAINTS,
    PLANNER_VERSION,
    PLAN_SCHEMA_VERSION,
    PlannerConfig,
)
from .errors import PlannerContractError
from .io import _read_json
from .task_builders import (
    _add_reporter_task,
    _apply_blocker_freeze,
    _design_iteration_tasks,
    _recommendation_tasks,
)
from .validation import _bind_exploration_decision, _validate_critic_report
import math

def _inject_project_config(state: dict, project_config: dict | None) -> None:
    """Bind an explicitly approved project config into the plan inputs."""
    if project_config is None:
        return
    state["project_config"] = project_config
    injected_project_id = str(project_config.get("project_id") or "").strip()
    state_project_id = str(state.get("project_id") or "").strip()
    if injected_project_id and state_project_id and injected_project_id != state_project_id:
        raise PlannerContractError(
            "planner_project_mismatch",
            "injected project config differs from State project ID",
        )

def _plan_workflow(state: dict, report: dict, report_sha: str) -> tuple[str, str, int]:
    """Resolve project ID, workflow ID, and source round for one plan."""
    source_round = int(state.get("round") or 1)
    project_id = str(
        report.get("source", {}).get("project_id")
        or state.get("project_id")
        or "unknown_project"
    ).strip()
    supplied_workflow_id = (
        state.get("workflow_id")
        or report.get("workflow_id")
        or report.get("source", {}).get("workflow_id")
    )
    if supplied_workflow_id:
        try:
            TraceContext(project_id=project_id, workflow_id=str(supplied_workflow_id))
        except ValueError as exc:
            raise PlannerContractError(
                "planner_workflow_id_invalid", "Planner received an invalid workflow_id"
            ) from exc
        workflow_id = str(supplied_workflow_id)
    else:
        workflow_id = derive_workflow_id(
            project_id, report["report_id"], report_sha, source_round
        )
    return project_id, workflow_id, source_round

def _plan_execution_status(
    verdict: str,
    tasks: list[dict],
    blocked_tasks: list[str],
    required_approval_tasks: list[str],
) -> str:
    if verdict == "blocked":
        return "recovery_only"
    if blocked_tasks:
        return "blocked"
    if required_approval_tasks:
        return "awaiting_approval"
    if tasks:
        return "ready"
    return "no_action"

def _plan_input_digest(
    report_path: Path,
    report_sha: str,
    workflow_id: str,
    state: dict,
    source_round: int,
    budgets: dict,
    config: PlannerConfig,
    decision_binding: dict[str, str] | None = None,
) -> str:
    inputs = {
        "critic_report_path": str(report_path),
        "critic_report_sha256": report_sha,
        "workflow_id": workflow_id,
        "state": {
            "project_id": state.get("project_id"),
            "round": source_round,
            "design_budget": budgets,
            "project_config_digest": object_sha256(
                state.get("project_config") or {}
            ),
            "critic_report_id": (state.get("critic") or {}).get("report_id")
            if isinstance(state.get("critic"), dict) else None,
        },
        "config": asdict(config),
        "planner_version": PLANNER_VERSION,
    }
    if decision_binding is not None:
        inputs["exploration_decision"] = {
            "decision_id": decision_binding["decision_id"],
            "decision_sha256": decision_binding["decision_sha256"],
        }
    return object_sha256(inputs)


def build_plan(
    *,
    critic_report_path: str | Path,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
    exploration_decision: Mapping | None = None,
) -> dict:
    """Purely convert one frozen Critic report into an execution plan.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7); it takes precedence over the project entry
    carried in ``state`` and must agree with ``state``'s ``project_id``.
    When omitted, behaviour is unchanged.

    ``exploration_decision`` optionally binds one validated E2 Decision into
    plan provenance and identity. Its adjustment may narrow only iterate-design
    peptide lengths; omitting it preserves the legacy source and digest shape.

    The injected config must also be injected into Execution (or carried in
    State), which re-verifies the digest and fails closed with
    ``project_config_drift`` on mismatch.
    """
    config = config or PlannerConfig()
    state = dict(state if state is not None else State.load())
    state.pop("_frozen_exploration_decision", None)
    validated_decision = (
        ExplorationDecision.from_dict(exploration_decision)
        if exploration_decision is not None
        else None
    )
    canonical_decision = (
        validated_decision.to_dict() if validated_decision is not None else None
    )
    _inject_project_config(state, project_config)
    report_path = Path(critic_report_path).expanduser().resolve()
    report = _read_json(report_path, "critic_report")
    report_sha = file_sha256(report_path)
    _validate_critic_report(report, state, report_sha)
    budgets, total_design_budget = _budget_snapshot(state)
    project_id, workflow_id, source_round = _plan_workflow(state, report, report_sha)
    decision_binding = (
        _bind_exploration_decision(
            validated_decision,
            canonical_decision,
            report=report,
            state=state,
            project_id=project_id,
            workflow_id=workflow_id,
            source_round=source_round,
        )
        if validated_decision is not None and canonical_decision is not None
        else None
    )
    issues_by_code = {issue["code"]: issue for issue in report["issues"]}
    tasks: list[dict] = []
    _design_iteration_tasks(
        tasks, state, report, report_sha, budgets, total_design_budget,
        config, issues_by_code,
    )
    _recommendation_tasks(tasks, report, issues_by_code, config)
    _add_reporter_task(tasks, report)
    _apply_blocker_freeze(tasks, report["verdict"])
    return _assemble_plan(
        tasks,
        report=report,
        report_path=report_path,
        report_sha=report_sha,
        state=state,
        config=config,
        project_id=project_id,
        workflow_id=workflow_id,
        source_round=source_round,
        budgets=budgets,
        total_design_budget=total_design_budget,
        decision_binding=decision_binding,
    )

def _plan_governance(
    tasks: list[dict], verdict: str, source_round: int
) -> dict:
    """Derive task/status governance for the assembled plan."""
    blocked_tasks = [
        task["task_id"] for task in tasks
        if task["execution_gate"]["status"] == "blocked"
        and task["disposition"] != "optional"
    ]
    required_approval_tasks = [
        task["task_id"] for task in tasks
        if task["approval"]["required"]
        and task["disposition"] != "optional"
        and task["execution_gate"]["status"] == "proposed"
    ]
    optional_task_ids = [
        task["task_id"] for task in tasks if task["disposition"] == "optional"
    ]
    status = _plan_execution_status(
        verdict, tasks, blocked_tasks, required_approval_tasks
    )
    has_required_iteration = any(
        task["phase"] in {"design", "evaluate", "iterate"}
        and task["disposition"] != "optional"
        for task in tasks
    )
    return {
        "status": status,
        "blocked_tasks": blocked_tasks,
        "required_approval_tasks": required_approval_tasks,
        "optional_task_ids": optional_task_ids,
        "target_round": source_round + 1 if has_required_iteration else source_round,
    }


def _plan_budget_request(
    tasks: list[dict], budgets: dict, total_design_budget: int
) -> dict:
    """Assemble the immutable budget request snapshot."""
    return {
        "configured_design_budget_snapshot": budgets,
        "configured_design_budget_total": total_design_budget,
        "requested_design_proposals": sum(
            task["resource_request"]["proposal_count"] for task in tasks
        ),
        "requested_gpu_job_slots": sum(
            task["resource_request"]["gpu_job_slots"] for task in tasks
            if task["disposition"] != "optional"
        ),
        "gpu_minutes": None,
        "gpu_minutes_status": "benchmark_required",
        "reservation_status": "not_reserved",
    }


def _plan_approval_request(
    required_approval_tasks: list[str], optional_task_ids: list[str]
) -> dict:
    """Assemble the approval request contract for the plan."""
    return {
        "artifact_required": bool(required_approval_tasks),
        "required_task_ids": required_approval_tasks,
        "optional_task_ids": optional_task_ids,
        "approval_schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_must_bind_plan_sha256": True,
    }


def _plan_execution(tasks: list[dict], blocked_tasks: list[str]) -> dict:
    """Assemble the execution policy for the plan."""
    return {
        "automatic_dispatch_allowed": False,
        "blocked_task_ids": blocked_tasks,
        "entry_task_ids": [
            task["task_id"] for task in tasks
            if not task["depends_on"]
            and task["execution_gate"]["status"] == "proposed"
        ],
        "orchestrator_required": True,
    }


def _compute_plan_metadata(tasks: list[dict], budgets: dict[str, int], config: PlannerConfig, state: dict[str, object]) -> dict[str, object]:
    """Attach lightweight compute-aware estimates directly to the assembled plan.

    Priority for `global_budget_minutes` (higher wins):
    1. `state["compute_budget"]["global_budget_minutes"]` if it's a mapping and finite
    2. `state["planning_constraints"]["global_budget_minutes"]` if mapping and finite
    3. `config.global_budget_minutes` (PlannerConfig)

    Accesses are defensive: non-mapping values in State are ignored to avoid
    raising `AttributeError` when callers store unexpected types.
    """
    total_estimated_gpu_minutes = 0.0
    # Use PlannerConfig tunables to compute conservative estimates. These are
    # priors and should be replaced by benchmark-driven values when available.
    per_proposal = float(config.gpu_minutes_per_proposal)
    candidate_factor = float(config.gpu_minutes_per_candidate_factor)
    prediction_minutes_per_candidate = int(
        config.prediction_gpu_slot_minutes_per_candidate
    )
    per_minute_cost = float(config.gpu_cost_per_minute_usd)

    for task in tasks:
        resource = task.get("resource_request") or {}
        resource_class = resource.get("class")
        if resource_class == "gpu":
            proposals = int(resource.get("proposal_count") or 0)
            candidates = int(resource.get("candidate_limit") or 0)
            if task.get("action") == "evaluate_new_design_candidates":
                estimated_minutes = candidates * prediction_minutes_per_candidate
            else:
                estimated_minutes = proposals * per_proposal + candidates * (
                    per_proposal * candidate_factor
                )
            estimated_cost = round(estimated_minutes * per_minute_cost, 4)
            resource["estimated_gpu_minutes"] = float(estimated_minutes)
            resource["estimated_cost_usd"] = float(estimated_cost)
            resource["estimate_status"] = "estimated"
            total_estimated_gpu_minutes += estimated_minutes
        else:
            resource["estimated_gpu_minutes"] = None
            resource["estimated_cost_usd"] = 0.0
            resource["estimate_status"] = "not_applicable"
        task["resource_request"] = resource

    route_resource_estimate = {
        key: int(value)
        for key, value in sorted((budgets or {}).items())
        if key.startswith("route_")
    }

    # Collect candidates defensively
    candidates: list[object] = []
    cb = state.get("compute_budget")
    if isinstance(cb, dict):
        candidates.append(cb.get("global_budget_minutes"))
    pc = state.get("planning_constraints")
    if isinstance(pc, dict):
        candidates.append(pc.get("global_budget_minutes"))
    candidates.append(config.global_budget_minutes)

    global_budget_minutes = None
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            v = float(candidate)
        except (TypeError, ValueError):
            continue
        # Reject NaN/Inf carried in State; only accept finite numbers
        if not math.isfinite(v):
            continue
        global_budget_minutes = v
        break

    return {
        "route_resource_estimate": route_resource_estimate,
        "max_rounds": int(config.max_rounds),
        "task_timeout_minutes": int(config.task_timeout_minutes),
        "global_budget_minutes": (
            float(global_budget_minutes) if global_budget_minutes is not None else None
        ),
        "on_budget_exhausted": config.on_budget_exhausted,
        # Advisory budget status: 'within_budget' | 'exceeds_budget' | 'unknown'
        "budget_status": (
            "unknown"
            if global_budget_minutes is None
            else ("exceeds_budget" if total_estimated_gpu_minutes > float(global_budget_minutes) else "within_budget")
        ),
        "total_estimated_gpu_minutes": float(total_estimated_gpu_minutes),
        "estimator_version": "simple-v1",
    }


def _assemble_plan(
    tasks: list[dict],
    *,
    report: dict,
    report_path: Path,
    report_sha: str,
    state: dict,
    config: PlannerConfig,
    project_id: str,
    workflow_id: str,
    source_round: int,
    budgets: dict,
    total_design_budget: int,
    decision_binding: dict[str, str] | None = None,
) -> dict:
    governance = _plan_governance(tasks, report["verdict"], source_round)
    input_digest = _plan_input_digest(
        report_path, report_sha, workflow_id, state, source_round, budgets, config,
        decision_binding,
    )
    plan_id = f"planner_{input_digest[:12]}"
    status = governance["status"]
    blocked_tasks = governance["blocked_tasks"]
    required_approval_tasks = governance["required_approval_tasks"]
    optional_task_ids = governance["optional_task_ids"]
    target_round = governance["target_round"]
    summary = (
        f"Planner converted Critic verdict={report['verdict']} into {len(tasks)} task(s): "
        f"status={status}; required approvals={len(required_approval_tasks)}; "
        f"blocked tasks={len(blocked_tasks)}; optional tasks={len(optional_task_ids)}."
    )
    source = {
        "critic_report": str(report_path),
        "critic_report_sha256": report_sha,
        "critic_report_id": report["report_id"],
        "critic_verdict": report["verdict"],
        "prediction_run_id": report["source"].get("prediction_run_id"),
        "project_id": report["source"].get("project_id"),
        "workflow_id": workflow_id,
    }
    if decision_binding is not None:
        source.update(
            {
                "exploration_decision_id": decision_binding["decision_id"],
                "exploration_decision_sha256": decision_binding["decision_sha256"],
                "exploration_decision_input_digest": decision_binding[
                    "decision_input_digest"
                ],
            }
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "input_digest": input_digest,
        "source": source,
        "status": status,
        "summary": summary,
        "cycle": {
            "source_round": source_round,
            "target_round": target_round,
            "round_advancement_deferred_to_orchestrator": True,
        },
        "budget_request": _plan_budget_request(tasks, budgets, total_design_budget),
        "policy_constraints": sorted(MANDATORY_POLICY_CONSTRAINTS),
        "approval_request": _plan_approval_request(
            required_approval_tasks, optional_task_ids
        ),
        "execution": _plan_execution(tasks, blocked_tasks),
        "tasks": tasks,
        "decision_metadata": _compute_plan_metadata(tasks, budgets, config, state),
    }
