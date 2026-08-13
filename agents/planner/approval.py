"""approval - split from agents/planner.py (PR6)."""

from __future__ import annotations

from contracts.action import get_action_spec
from contracts.trace import TraceContext, derive_workflow_id
from data_layer import EvidenceLogger
from datetime import datetime, timezone
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from .errors import PlannerContractError
from .io import _atomic_json, _read_json
from contracts.plan import (
    validate_approval_gpu_minutes,
    validate_plan_for_approval,
    validate_sha256,
)

from .config import APPROVAL_SCHEMA_VERSION, PLAN_ID_RE

def _approval(
    *,
    action: str,
    critic_approval_required: bool,
    data_integrity: bool = False,
) -> dict:
    resource_class = get_action_spec(action).resource_class
    types = []
    if resource_class == "gpu":
        types.append("execution_budget")
    if critic_approval_required:
        types.append("scientific_policy")
    if data_integrity:
        types.append("data_integrity")
    return {
        "required": bool(types),
        "types": types,
        "status": "pending" if types else "not_required",
    }

def _validate_approval_scope(plan: dict, task_ids: list[str]) -> list[str]:
    """Selected tasks must exist, request approval, and be unblocked."""
    selected = sorted(set(str(task_id).strip() for task_id in task_ids if str(task_id).strip()))
    if not selected:
        raise PlannerContractError(
            "approval_scope_required", "approval requires explicit task IDs"
        )
    tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    unknown = sorted(set(selected) - set(tasks_by_id))
    if unknown:
        raise PlannerContractError(
            "approval_task_unknown", f"approval references unknown tasks: {unknown}"
        )
    non_approvable = [
        task_id for task_id in selected
        if not tasks_by_id[task_id].get("approval", {}).get("required")
    ]
    if non_approvable:
        raise PlannerContractError(
            "approval_task_not_required",
            f"tasks do not request approval: {non_approvable}",
        )
    blocked = [
        task_id for task_id in selected
        if tasks_by_id[task_id].get("execution_gate", {}).get("status") == "blocked"
    ]
    if blocked:
        raise PlannerContractError(
            "approval_task_blocked", f"blocked tasks cannot be approved: {blocked}"
        )
    return selected


def _validate_approval_budget(
    plan: dict,
    selected: list[str],
    max_gpu_job_slots: int | None,
    max_gpu_minutes: float | None,
    max_design_proposals: int | None,
    max_prediction_candidates: int | None,
) -> None:
    """The human budget must cover every selected GPU resource request."""
    tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    gpu_tasks = [
        tasks_by_id[task_id] for task_id in selected
        if tasks_by_id[task_id]["resource_request"]["class"] == "gpu"
    ]
    if gpu_tasks:
        required_concurrent_slots = max(
            task["resource_request"]["gpu_job_slots"] for task in gpu_tasks
        )
        if max_gpu_job_slots is None or max_gpu_job_slots < required_concurrent_slots:
            raise PlannerContractError(
                "approval_gpu_limit_insufficient",
                "max_gpu_job_slots must cover the maximum concurrent task request",
            )
        validate_approval_gpu_minutes(
            plan,
            selected,
            max_gpu_minutes,
            error_cls=PlannerContractError,
        )
    requested_proposals = sum(
        task["resource_request"]["proposal_count"] for task in gpu_tasks
    )
    if requested_proposals and (
        max_design_proposals is None or max_design_proposals < requested_proposals
    ):
        raise PlannerContractError(
            "approval_design_limit_insufficient",
            "max_design_proposals is lower than the selected plan request",
        )
    requested_predictions = sum(
        task["resource_request"]["candidate_limit"] for task in gpu_tasks
        if task["agent"] in {"prediction", "prediction/design", "design/prediction"}
    )
    if requested_predictions and (
        max_prediction_candidates is None
        or max_prediction_candidates < requested_predictions
    ):
        raise PlannerContractError(
            "approval_prediction_limit_insufficient",
            "max_prediction_candidates is lower than the selected plan request",
        )


def _persist_approval(
    output_path: str | Path | None,
    plan_path: Path,
    semantic: dict,
    approval_id: str,
) -> tuple[dict, str, Path]:
    """Write a new approval file or idempotently reuse identical content."""
    if output_path is None:
        output_path = plan_path.parent / "approvals" / f"{approval_id}.json"
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        existing = _read_json(output_path, "planner_approval")
        existing_semantic = {
            key: existing.get(key) for key in semantic
        }
        if existing_semantic != semantic or existing.get("approval_id") != approval_id:
            raise PlannerContractError(
                "approval_output_conflict", "approval path contains different content"
            )
        return existing, file_sha256(output_path), output_path
    approval = dict(semantic)
    approval.update({
        "approval_id": approval_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authorization_semantics": (
            "Only approved_task_ids within budget_limits may be dispatched; "
            "any plan content change invalidates this approval."
        ),
    })
    _atomic_json(output_path, approval)
    return approval, file_sha256(output_path), output_path


def _load_approvable_plan(plan_path: str | Path) -> tuple[dict, str, str]:
    """Load a plan whose ID is bound to its immutable input digest."""
    plan_path = Path(plan_path).expanduser().resolve()
    plan = _read_json(plan_path, "planner_plan")
    plan = validate_plan_for_approval(plan, plan_path, error_cls=PlannerContractError)
    plan_id = str(plan.get("plan_id") or "")
    input_digest = validate_sha256(
        plan.get("input_digest"), "plan_input_digest_invalid", "plan input_digest",
        error_cls=PlannerContractError,
    )
    if not PLAN_ID_RE.fullmatch(plan_id) or plan_id != f"planner_{input_digest[:12]}":
        raise PlannerContractError("plan_id_invalid", "plan ID is not bound to input_digest")
    return plan, plan_id, file_sha256(plan_path)


def record_approval(
    *,
    plan_path: str | Path,
    task_ids: list[str],
    approver: str,
    justification: str,
    max_gpu_job_slots: int | None = None,
    max_gpu_minutes: float | None = None,
    max_design_proposals: int | None = None,
    max_prediction_candidates: int | None = None,
    output_path: str | Path | None = None,
) -> dict:
    """Record explicit human approval bound to one immutable plan digest."""
    plan_path = Path(plan_path).expanduser().resolve()
    plan, plan_id, plan_sha = _load_approvable_plan(plan_path)
    approver = str(approver or "").strip()
    justification = str(justification or "").strip()
    if not approver or not justification:
        raise PlannerContractError(
            "approval_identity_required", "approver and justification are required"
        )
    selected = _validate_approval_scope(plan, task_ids)
    _validate_approval_budget(
        plan,
        selected,
        max_gpu_job_slots,
        max_gpu_minutes,
        max_design_proposals,
        max_prediction_candidates,
    )
    semantic = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "project_id": (plan.get("source") or {}).get("project_id"),
        "approved_task_ids": selected,
        "approver": approver,
        "justification": justification,
        "budget_limits": {
            "max_gpu_job_slots": max_gpu_job_slots,
            "max_gpu_minutes": max_gpu_minutes,
            "max_design_proposals": max_design_proposals,
            "max_prediction_candidates": max_prediction_candidates,
        },
    }
    approval_id = f"approval_{object_sha256(semantic)[:12]}"
    approval, approval_sha, approval_path = _persist_approval(
        output_path, plan_path, semantic, approval_id
    )
    if not any(
        entry.get("event_type") == "planner_approval_recorded"
        and entry.get("approval_id") == approval_id
        for entry in EvidenceLogger.get_all()
    ):
        EvidenceLogger.planner_approval_recorded(
            approval_id=approval_id,
            approval_path=str(approval_path),
            approval_sha256=approval_sha,
            plan_id=plan_id,
            plan_sha256=plan_sha,
            approved_task_ids=selected,
            approver=approver,
            budget_limits=semantic["budget_limits"],
            trace_context=TraceContext(
                project_id=str(semantic.get("project_id") or "unknown_project"),
                workflow_id=str(plan.get("workflow_id") or derive_workflow_id(
                    str(semantic.get("project_id") or "unknown_project"),
                    str(plan.get("source", {}).get("critic_report_id") or plan_id),
                    str(plan.get("source", {}).get("critic_report_sha256") or plan_sha),
                    plan.get("cycle", {}).get("source_round", 1),
                )),
                plan_id=plan_id,
            ),
        )
    return {
        "approval": approval,
        "approval_path": str(approval_path),
        "approval_sha256": approval_sha,
    }

