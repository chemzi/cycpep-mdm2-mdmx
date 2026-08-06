"""plan_builder - split from agents/planner/service.py (PR6)."""

from __future__ import annotations

from contracts.trace import derive_workflow_id
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
from .validation import _validate_critic_report

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
) -> str:
    return object_sha256({
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
    })

def build_plan(
    *,
    critic_report_path: str | Path,
    state: dict | None = None,
    config: PlannerConfig | None = None,
    project_config: dict | None = None,
) -> dict:
    """Purely convert one frozen Critic report into an execution plan.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7); it takes precedence over the project entry
    carried in ``state`` and must agree with ``state``'s ``project_id``.
    When omitted, behaviour is unchanged.

    The injected config must also be injected into Execution (or carried in
    State), which re-verifies the digest and fails closed with
    ``project_config_drift`` on mismatch.
    """
    config = config or PlannerConfig()
    state = dict(state if state is not None else State.load())
    _inject_project_config(state, project_config)
    report_path = Path(critic_report_path).expanduser().resolve()
    report = _read_json(report_path, "critic_report")
    report_sha = file_sha256(report_path)
    _validate_critic_report(report, state, report_sha)
    budgets, total_design_budget = _budget_snapshot(state)
    project_id, workflow_id, source_round = _plan_workflow(state, report, report_sha)
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
) -> dict:
    governance = _plan_governance(tasks, report["verdict"], source_round)
    input_digest = _plan_input_digest(
        report_path, report_sha, workflow_id, state, source_round, budgets, config
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
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "workflow_id": workflow_id,
        "input_digest": input_digest,
        "source": {
            "critic_report": str(report_path),
            "critic_report_sha256": report_sha,
            "critic_report_id": report["report_id"],
            "critic_verdict": report["verdict"],
            "prediction_run_id": report["source"].get("prediction_run_id"),
            "project_id": report["source"].get("project_id"),
            "workflow_id": workflow_id,
        },
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
    }
