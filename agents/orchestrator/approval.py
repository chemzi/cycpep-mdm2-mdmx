"""approval - split from agents/orchestrator/service.py (PR6)."""

from __future__ import annotations

import math
from agents.planner import APPROVAL_SCHEMA_VERSION
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from .errors import OrchestratorContractError
from .io import _read_json

def _task_map(plan: dict) -> dict[str, dict]:
    return {task["task_id"]: task for task in plan["tasks"]}

def _approval_semantic(approval: dict) -> dict:
    limits = approval.get("budget_limits")
    if not isinstance(limits, dict):
        raise OrchestratorContractError(
            "approval_budget_invalid", "approval budget_limits must be an object"
        )
    required_limit_keys = {
        "max_gpu_job_slots",
        "max_gpu_minutes",
        "max_design_proposals",
        "max_prediction_candidates",
    }
    if set(limits) != required_limit_keys:
        raise OrchestratorContractError(
            "approval_budget_invalid",
            f"approval budget keys must be {sorted(required_limit_keys)}",
        )
    return {
        "schema_version": approval.get("schema_version"),
        "plan_id": approval.get("plan_id"),
        "plan_path": approval.get("plan_path"),
        "plan_sha256": approval.get("plan_sha256"),
        "project_id": approval.get("project_id"),
        "approved_task_ids": approval.get("approved_task_ids"),
        "approver": approval.get("approver"),
        "justification": approval.get("justification"),
        "budget_limits": limits,
    }

def _validate_approval_binding(
    approval: dict,
    semantic: dict,
    plan_path: Path,
    plan: dict,
    plan_sha256: str,
) -> None:
    """Approval content must bind to the exact plan it approves."""
    if semantic["schema_version"] != APPROVAL_SCHEMA_VERSION:
        raise OrchestratorContractError(
            "approval_schema_unsupported", "unsupported approval schema"
        )
    expected_id = f"approval_{object_sha256(semantic)[:12]}"
    if approval.get("approval_id") != expected_id:
        raise OrchestratorContractError(
            "approval_id_mismatch", "approval ID is not bound to its content"
        )
    if semantic["plan_id"] != plan.get("plan_id"):
        raise OrchestratorContractError(
            "approval_plan_mismatch", "approval references a different plan ID"
        )
    if semantic["plan_sha256"] != plan_sha256:
        raise OrchestratorContractError(
            "approval_plan_hash_mismatch", "approval references a different plan SHA-256"
        )
    if Path(str(semantic["plan_path"])).expanduser().resolve() != plan_path:
        raise OrchestratorContractError(
            "approval_plan_path_mismatch", "approval references a different plan path"
        )
    if semantic["project_id"] != (plan.get("source") or {}).get("project_id"):
        raise OrchestratorContractError(
            "approval_project_mismatch", "approval project differs from plan"
        )
    if not str(semantic.get("approver") or "").strip() or not str(
        semantic.get("justification") or ""
    ).strip():
        raise OrchestratorContractError(
            "approval_identity_missing", "approval lacks approver or justification"
        )

def _validate_approval_scope(task_ids: list[str], tasks: dict[str, dict]) -> None:
    """Approved tasks must exist, be unblocked, and have requested approval."""
    unknown = sorted(set(task_ids) - set(tasks))
    if unknown:
        raise OrchestratorContractError(
            "approval_task_unknown", f"approval references unknown tasks: {unknown}"
        )
    blocked = [
        task_id for task_id in task_ids
        if tasks[task_id]["execution_gate"]["status"] == "blocked"
    ]
    if blocked:
        raise OrchestratorContractError(
            "approval_task_blocked", f"approval covers blocked tasks: {blocked}"
        )
    nonrequested = [
        task_id for task_id in task_ids
        if not tasks[task_id]["approval"]["required"]
    ]
    if nonrequested:
        raise OrchestratorContractError(
            "approval_task_not_required", f"tasks did not request approval: {nonrequested}"
        )

def _validate_gpu_limits(gpu_tasks: list[dict], limits: dict) -> None:
    """Approval budget limits must cover the GPU tasks' resource requests."""
    slots = limits["max_gpu_job_slots"]
    minutes = limits["max_gpu_minutes"]
    required_concurrent_slots = max(
        task["resource_request"]["gpu_job_slots"] for task in gpu_tasks
    )
    if (
        not isinstance(slots, int)
        or isinstance(slots, bool)
        or slots < required_concurrent_slots
    ):
        raise OrchestratorContractError(
            "approval_gpu_slots_insufficient", "approval GPU slots are insufficient"
        )
    try:
        minute_limit = float(minutes)
    except (TypeError, ValueError) as exc:
        raise OrchestratorContractError(
            "approval_gpu_minutes_invalid", "approval lacks GPU minute ceiling"
        ) from exc
    if not math.isfinite(minute_limit) or minute_limit <= 0:
        raise OrchestratorContractError(
            "approval_gpu_minutes_invalid", "approval GPU minute ceiling must be positive"
        )
    proposals = sum(task["resource_request"]["proposal_count"] for task in gpu_tasks)
    proposal_limit = limits["max_design_proposals"]
    if proposals and (
        not isinstance(proposal_limit, int)
        or isinstance(proposal_limit, bool)
        or proposal_limit < proposals
    ):
        raise OrchestratorContractError(
            "approval_design_limit_insufficient", "approval proposal limit is insufficient"
        )
    predictions = sum(
        task["resource_request"]["candidate_limit"] for task in gpu_tasks
        if task["agent"] in {"prediction", "prediction/design", "design/prediction"}
    )
    prediction_limit = limits["max_prediction_candidates"]
    if predictions and (
        not isinstance(prediction_limit, int)
        or isinstance(prediction_limit, bool)
        or prediction_limit < predictions
    ):
        raise OrchestratorContractError(
            "approval_prediction_limit_insufficient",
            "approval Prediction candidate limit is insufficient",
        )

def _validate_approval(
    approval_path: str | Path,
    *,
    plan_path: Path,
    plan: dict,
    plan_sha256: str,
) -> dict:
    path = Path(approval_path).expanduser().resolve()
    approval = _read_json(path, "planner_approval")
    semantic = _approval_semantic(approval)
    _validate_approval_binding(approval, semantic, plan_path, plan, plan_sha256)
    task_ids = semantic.get("approved_task_ids")
    if not isinstance(task_ids, list) or not task_ids or len(task_ids) != len(set(task_ids)):
        raise OrchestratorContractError(
            "approval_scope_invalid", "approval task IDs must be a non-empty unique array"
        )
    tasks = _task_map(plan)
    _validate_approval_scope(task_ids, tasks)
    gpu_tasks = [
        tasks[task_id] for task_id in task_ids
        if tasks[task_id]["resource_request"]["class"] == "gpu"
    ]
    limits = semantic["budget_limits"]
    if gpu_tasks:
        _validate_gpu_limits(gpu_tasks, limits)
    return {
        "approval_id": approval["approval_id"],
        "approval_path": str(path),
        "approval_sha256": file_sha256(path),
        "approved_task_ids": sorted(task_ids),
        "approver": semantic["approver"],
        "justification": semantic["justification"],
        "budget_limits": limits,
    }

def _authorization_for_task(run: dict, task_id: str) -> dict | None:
    for approval in run.get("approvals", []):
        if task_id in approval.get("approved_task_ids", []):
            return approval
    return None

def _add_approval_in_memory(
    run: dict,
    approval_path: str | Path,
    *,
    plan_path: Path,
    plan: dict,
    plan_sha256: str,
) -> tuple[dict, bool]:
    value = _validate_approval(
        approval_path,
        plan_path=plan_path,
        plan=plan,
        plan_sha256=plan_sha256,
    )
    existing = next(
        (
            item for item in run.get("approvals", [])
            if item["approval_id"] == value["approval_id"]
        ),
        None,
    )
    if existing:
        if existing != value:
            raise OrchestratorContractError(
                "run_approval_conflict", "run contains conflicting approval metadata"
            )
        return existing, False
    existing_tasks = {
        task_id
        for item in run.get("approvals", [])
        for task_id in item.get("approved_task_ids", [])
    }
    overlap = sorted(existing_tasks.intersection(value["approved_task_ids"]))
    if overlap:
        raise OrchestratorContractError(
            "approval_scope_overlap",
            f"tasks already covered by another approval: {overlap}",
        )
    run.setdefault("approvals", []).append(value)
    run["approvals"].sort(key=lambda item: item["approval_id"])
    return value, True
