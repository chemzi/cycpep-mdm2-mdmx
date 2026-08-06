"""state_machine - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

from contracts.errors import ErrorInfo
from contracts.task import TERMINAL_TASK_STATUSES, TaskStatus
from contracts.trace import TraceContext
from data_layer import EvidenceLogger, State
from pathlib import Path
from prediction_pipeline.contracts import file_sha256
from typing import Any
from .claim import _validate_claim
from .errors import OrchestratorContractError
from .io import _atomic_json, _read_json, _run_lock, _utcnow
from .lease import (
    _acquire_global_gpu_lease,
    _consume_gpu_usage,
    _release_global_gpu_lease,
)
from .service import (
    _add_approval_in_memory,
    _authorization_for_task,
    _refresh,
    _sync_state,
    _task_map,
    _trace_for_run,
    _validate_run_binding,
    _workflow_id_for_plan,
)

def authorize(*, run_path: str | Path, approval_path: str | Path) -> dict:
    """Attach one previously issued approval artifact to an existing run."""
    run_path = Path(run_path).expanduser().resolve()
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        plan_path, plan, plan_sha = _validate_run_binding(run, run_path)
        approval, added = _add_approval_in_memory(
            run,
            approval_path,
            plan_path=plan_path,
            plan=plan,
            plan_sha256=plan_sha,
        )
        _refresh(run, plan)
        _atomic_json(run_path, run)
    _sync_state(run, plan)
    if added:
        EvidenceLogger.log("orchestrator", "orchestrator_approval_loaded", {
            "run_id": run["run_id"],
            "approval_id": approval["approval_id"],
            "approval_sha256": approval["approval_sha256"],
            "approved_task_ids": approval["approved_task_ids"],
        }, phase="iterate", trace_context=_trace_for_run(run))
    return {"run": run, "run_path": str(run_path), "approval_added": added}

def _resolve_failure(
    error_info: ErrorInfo | None,
    reason: str | None,
    retryable: bool | None,
) -> tuple[ErrorInfo, str]:
    """Normalize failure reason and ErrorInfo, rejecting conflicts."""
    reason = str(reason or "").strip()
    if error_info is None:
        if not reason:
            raise OrchestratorContractError("failure_reason_required", "failure reason is required")
        error_info = ErrorInfo(
            code="orchestrator_task_failed",
            message=reason,
            component="orchestrator",
            retryable=bool(retryable),
        )
    else:
        if retryable is not None and retryable != error_info.retryable:
            raise OrchestratorContractError(
                "failure_retryability_conflict",
                "retryable argument conflicts with ErrorInfo",
            )
        if not reason:
            reason = f"{error_info.code}: {error_info.message}"
    return error_info, reason

def _fail_gpu_cleanup(
    run: dict,
    task: dict,
    task_id: str,
    claim_token: str,
    gpu_minutes: float | None,
) -> tuple[dict[str, Any], bool]:
    """Consume GPU usage and clear the run's GPU lease for a failed task."""
    usage: dict[str, Any] = {}
    if task["resource_request"]["class"] == "gpu":
        # A failed process must always be closable and release its lease,
        # even when its measured runtime exceeded the approved ceiling.
        usage = _consume_gpu_usage(
            run, task_id, gpu_minutes, enforce_limit=False
        )
        lease = run["resources"].get("gpu_lease")
        if not lease or lease.get("claim_token") != claim_token:
            raise OrchestratorContractError(
                "gpu_lease_mismatch", "failed GPU task does not own GPU lease"
            )
        run["resources"]["gpu_lease"] = None
        return usage, True
    if gpu_minutes is not None:
        raise OrchestratorContractError(
            "gpu_minutes_unexpected", "CPU task cannot report GPU minutes"
        )
    return usage, False

def fail(
    *,
    run_path: str | Path,
    task_id: str,
    claim_token: str,
    reason: str | None,
    retryable: bool | None = None,
    error_info: ErrorInfo | None = None,
    gpu_minutes: float | None = None,
) -> dict:
    """Fail a claimed task; retry is recorded but never scheduled automatically."""
    run_path = Path(run_path).expanduser().resolve()
    error_info, reason = _resolve_failure(error_info, reason, retryable)
    release_global_gpu = False
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        state = _validate_claim(run, task_id, claim_token)
        task = _task_map(plan)[task_id]
        usage, release_global_gpu = _fail_gpu_cleanup(
            run, task, task_id, claim_token, gpu_minutes
        )
        error = {
            "reason": reason,
            **error_info.to_dict(),
            "retryable": bool(error_info.retryable),
            "automatic_retry_scheduled": False,
            "failed_at": _utcnow(),
        }
        state.update({
            "status": TaskStatus.FAILED.value,
            "claim": None,
            "last_error": error,
            "resource_usage": usage,
        })
        history = state.setdefault("attempt_history", [])
        if history:
            history[-1].update({
                "status": TaskStatus.FAILED.value,
                "failed_at": error["failed_at"],
                "error": error,
            })
        _refresh(run, plan)
        _atomic_json(run_path, run)
    if release_global_gpu:
        _release_global_gpu_lease(claim_token)
    _sync_state(run, plan)
    EvidenceLogger.log("orchestrator", "orchestrator_task_failed", {
        "run_id": run["run_id"],
        "task_id": task_id,
        "error": error,
        "code": error["code"],
        "message": error["message"],
        "component": error["component"],
        "retryable": error["retryable"],
        "resource_usage": usage,
        "run_status": run["status"],
        "attempt": int(state.get("attempts") or 0),
        "attempt_id": TraceContext.attempt_id_for(
            task_id, int(state.get("attempts") or 0)
        ),
    }, phase=task["phase"], trace_context=_trace_for_run(
        run, task_id=task_id, attempt=int(state.get("attempts") or 0)
    ))
    return {"run": run, "run_path": str(run_path), "task_id": task_id}

def skip(*, run_path: str | Path, task_id: str, reason: str) -> dict:
    """Skip an optional unclaimed task; required tasks cannot be skipped."""
    run_path = Path(run_path).expanduser().resolve()
    reason = str(reason or "").strip()
    if not reason:
        raise OrchestratorContractError("skip_reason_required", "skip reason is required")
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        task = _task_map(plan).get(task_id)
        if task is None:
            raise OrchestratorContractError("task_unknown", f"unknown task {task_id}")
        if task["disposition"] != "optional":
            raise OrchestratorContractError(
                "required_task_skip_forbidden", f"task {task_id} is not optional"
            )
        state = run["tasks"][task_id]
        if state["status"] == TaskStatus.CLAIMED.value or state["status"] in TERMINAL_TASK_STATUSES:
            raise OrchestratorContractError(
                "task_skip_invalid", f"task {task_id} status is {state['status']}"
            )
        state.update({
            "status": TaskStatus.SKIPPED.value,
            "last_error": {"reason": reason, "skipped_at": _utcnow()},
            "completed_at": _utcnow(),
        })
        _refresh(run, plan)
        _atomic_json(run_path, run)
    _sync_state(run, plan)
    EvidenceLogger.log("orchestrator", "orchestrator_task_skipped", {
        "run_id": run["run_id"],
        "task_id": task_id,
        "reason": reason,
        "run_status": run["status"],
    }, phase=task["phase"], trace_context=_trace_for_run(run, task_id=task_id))
    return {"run": run, "run_path": str(run_path), "task_id": task_id}

def recover(
    *,
    run_path: str | Path,
    task_id: str,
    claim_token: str,
    operator: str,
    reason: str,
    process_stopped_confirmed: bool,
    gpu_minutes: float | None = None,
) -> dict:
    """Release an abandoned claim only after the worker process is confirmed stopped."""
    if not process_stopped_confirmed:
        raise OrchestratorContractError(
            "process_stop_confirmation_required",
            "recovery requires confirmation that the external process has stopped",
        )
    operator = str(operator or "").strip()
    reason = str(reason or "").strip()
    if not operator or not reason:
        raise OrchestratorContractError(
            "recovery_audit_required", "recovery requires operator and reason"
        )
    result = fail(
        run_path=run_path,
        task_id=task_id,
        claim_token=claim_token,
        reason=f"manual_recovery_by={operator}: {reason}",
        retryable=False,
        gpu_minutes=gpu_minutes,
    )
    EvidenceLogger.log("orchestrator", "orchestrator_claim_recovered", {
        "run_id": result["run"]["run_id"],
        "task_id": task_id,
        "operator": operator,
        "reason": reason,
        "process_stopped_confirmed": True,
    }, phase="iterate", trace_context=_trace_for_run(
        result["run"], task_id=task_id,
        attempt=int(result["run"]["tasks"][task_id].get("attempts") or 0),
    ))
    return result

def retry(
    *,
    run_path: str | Path,
    task_id: str,
    operator: str,
    reason: str,
) -> dict:
    """Explicitly requeue a retryable failed task; never automatic."""
    run_path = Path(run_path).expanduser().resolve()
    operator = str(operator or "").strip()
    reason = str(reason or "").strip()
    if not operator or not reason:
        raise OrchestratorContractError(
            "retry_audit_required", "retry requires operator and reason"
        )
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        task = _task_map(plan).get(task_id)
        if task is None:
            raise OrchestratorContractError("task_unknown", f"unknown task {task_id}")
        state = run["tasks"][task_id]
        if state.get("status") != TaskStatus.FAILED.value:
            raise OrchestratorContractError(
                "retry_status_invalid", f"task {task_id} is not failed"
            )
        last_error = state.get("last_error") or {}
        if last_error.get("retryable") is not True:
            raise OrchestratorContractError(
                "retry_not_allowed", f"task {task_id} failure is not retryable"
            )
        state["status"] = TaskStatus.PENDING_DEPENDENCY.value
        state["last_error"] = dict(last_error, retry_requested_by=operator,
                                    retry_reason=reason, retry_requested_at=_utcnow())
        _refresh(run, plan)
        _atomic_json(run_path, run)
    _sync_state(run, plan)
    attempt = int(run["tasks"][task_id].get("attempts") or 0)
    EvidenceLogger.log("orchestrator", "orchestrator_task_retry_requested", {
        "run_id": run["run_id"],
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": TraceContext.attempt_id_for(task_id, attempt),
        "operator": operator,
        "reason": reason,
        "status": run["tasks"][task_id]["status"],
    }, phase=task["phase"], trace_context=_trace_for_run(
        run, task_id=task_id, attempt=attempt
    ))
    return {"run": run, "run_path": str(run_path), "task_id": task_id}
