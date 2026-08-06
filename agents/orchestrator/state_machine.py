"""state_machine - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

import uuid
from contracts.errors import ErrorInfo
from contracts.task import TERMINAL_TASK_STATUSES, TaskStatus
from contracts.trace import TraceContext
from data_layer import EvidenceLogger, State
from execution.contracts import DISPATCH_SCHEMA_VERSION
from pathlib import Path
from prediction_pipeline.contracts import file_sha256
from typing import Any
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

def claim(*, run_path: str | Path, task_id: str, worker: str) -> dict:
    """Claim one ready task and emit an immutable worker dispatch packet."""
    run_path = Path(run_path).expanduser().resolve()
    worker = str(worker or "").strip()
    if not worker:
        raise OrchestratorContractError("worker_required", "worker identity is required")
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        workflow_id = run.get("workflow_id") or _workflow_id_for_plan(plan)
        run["workflow_id"] = workflow_id
        run.setdefault("plan", {}).setdefault("workflow_id", workflow_id)
        active = State.load().get("orchestrator") or {}
        if isinstance(active, dict) and active.get("run_id") not in {None, run["run_id"]}:
            raise OrchestratorContractError(
                "active_run_conflict", "State points to a different Orchestrator run"
            )
        current_round = int(State.load().get("round") or 1)
        allowed_rounds = {int(plan["cycle"]["source_round"])}
        if run.get("status") in {"completed_required", "completed"}:
            allowed_rounds.add(int(plan["cycle"]["target_round"]))
        if current_round not in allowed_rounds:
            raise OrchestratorContractError(
                "state_round_conflict", "State round changed after planning"
            )
        _refresh(run, plan)
        tasks = _task_map(plan)
        if task_id not in tasks:
            raise OrchestratorContractError("task_unknown", f"unknown task {task_id}")
        state = run["tasks"][task_id]
        if state["status"] != TaskStatus.READY.value:
            raise OrchestratorContractError(
                "task_not_ready", f"task {task_id} status is {state['status']}"
            )
        task = tasks[task_id]
        try:
            from execution.contracts import assert_action_executable

            assert_action_executable(task)
        except Exception as exc:
            raise OrchestratorContractError(
                getattr(exc, "code", "execution_action_invalid"), str(exc)
            ) from exc
        approval = _authorization_for_task(run, task_id)
        if task["approval"]["required"] and approval is None:
            raise OrchestratorContractError(
                "task_approval_missing", f"task {task_id} has no valid approval"
            )
        if task["resource_request"]["class"] == "gpu" and run["resources"]["gpu_lease"]:
            holder = run["resources"]["gpu_lease"]["task_id"]
            raise OrchestratorContractError(
                "gpu_lease_busy", f"single GPU lease is held by {holder}"
            )
        _verify_dependency_outputs(run, task)
        token = uuid.uuid4().hex
        attempt = state["attempts"] + 1
        claimed_at = _utcnow()
        claim_value = {
            "claim_token": token,
            "worker": worker,
            "claimed_at": claimed_at,
            "approval_id": approval["approval_id"] if approval else None,
        }
        global_lease = None
        if task["resource_request"]["class"] == "gpu":
            global_lease = {
                "run_id": run["run_id"],
                "run_path": str(run_path),
                "task_id": task_id,
                "claim_token": token,
                "worker": worker,
                "acquired_at": claimed_at,
            }
            _acquire_global_gpu_lease(global_lease)
        state.update({
            "status": TaskStatus.CLAIMED.value,
            "attempts": attempt,
            "claim": claim_value,
            "last_error": None,
        })
        state.setdefault("attempt_history", []).append({
            "attempt": attempt,
            "attempt_id": TraceContext.attempt_id_for(task_id, attempt),
            "worker": worker,
            "claimed_at": claimed_at,
            "status": TaskStatus.CLAIMED.value,
        })
        if task["resource_request"]["class"] == "gpu":
            run["resources"]["gpu_lease"] = {
                "task_id": task_id,
                "claim_token": token,
                "worker": worker,
                "acquired_at": claimed_at,
            }
        dependency_outputs = {
            dependency: run["tasks"][dependency]["outputs"]
            for dependency in task["depends_on"]
        }
        packet = {
            "schema_version": DISPATCH_SCHEMA_VERSION,
            "workflow_id": run.get("workflow_id") or plan.get("workflow_id"),
            "run_id": run["run_id"],
            "plan_id": plan["plan_id"],
            "plan_sha256": run["plan"]["plan_sha256"],
            "task": task,
            "task_attempt": attempt,
            "attempt_id": TraceContext.attempt_id_for(task_id, attempt),
            "trace_context": _trace_for_run(
                run, task_id=task_id, attempt=attempt
            ).to_dict(),
            "claim_token": token,
            "worker": worker,
            "claimed_at": claimed_at,
            "approval": approval,
            "dependency_outputs": dependency_outputs,
            "completion_contract": {
                "all_output_files_are_hashed": True,
                "gpu_minutes_required_for_gpu_task": (
                    task["resource_request"]["class"] == "gpu"
                ),
                "automatic_retry_allowed": False,
            },
        }
        packet_path = (
            run_path.parent / "dispatch" / task_id
            / f"attempt_{attempt}_{token[:8]}.json"
        )
        try:
            _atomic_json(packet_path, packet)
            state["claim"]["dispatch_packet_path"] = str(packet_path)
            state["claim"]["dispatch_packet_sha256"] = file_sha256(packet_path)
            _refresh(run, plan)
            _atomic_json(run_path, run)
        except Exception:
            if global_lease is not None:
                _release_global_gpu_lease(token)
            raise
    _sync_state(run, plan)
    EvidenceLogger.log("orchestrator", "orchestrator_task_claimed", {
        "run_id": run["run_id"],
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": TraceContext.attempt_id_for(task_id, attempt),
        "worker": worker,
        "dispatch_packet_path": str(packet_path),
        "dispatch_packet_sha256": file_sha256(packet_path),
        "resource_class": task["resource_request"]["class"],
    }, phase=task["phase"], trace_context=_trace_for_run(
        run, task_id=task_id, attempt=attempt
    ))
    return {
        "run": run,
        "run_path": str(run_path),
        "task_id": task_id,
        "workflow_id": run.get("workflow_id"),
        "attempt": attempt,
        "attempt_id": TraceContext.attempt_id_for(task_id, attempt),
        "claim_token": token,
        "dispatch_packet_path": str(packet_path),
        "dispatch_packet_sha256": file_sha256(packet_path),
    }

def _validate_claim(run: dict, task_id: str, claim_token: str) -> dict:
    state = (run.get("tasks") or {}).get(task_id)
    if state is None:
        raise OrchestratorContractError("task_unknown", f"unknown task {task_id}")
    if state.get("status") != TaskStatus.CLAIMED.value or not isinstance(state.get("claim"), dict):
        raise OrchestratorContractError(
            "task_not_claimed", f"task {task_id} has no active claim"
        )
    if state["claim"].get("claim_token") != claim_token:
        raise OrchestratorContractError(
            "claim_token_mismatch", f"claim token does not authorize task {task_id}"
        )
    return state

def _verify_dependency_outputs(run: dict, task: dict) -> None:
    for dependency in task["depends_on"]:
        state = run["tasks"][dependency]
        if state["status"] == TaskStatus.SKIPPED.value:
            continue
        if state["status"] != TaskStatus.SUCCEEDED.value or not state.get("outputs"):
            raise OrchestratorContractError(
                "dependency_output_missing",
                f"dependency {dependency} has no successful output inventory",
            )
        for artifact in state["outputs"]:
            path = Path(str(artifact.get("path") or "")).expanduser().resolve()
            if not path.is_file():
                raise OrchestratorContractError(
                    "dependency_output_missing", f"dependency output missing: {path}"
                )
            if file_sha256(path) != artifact.get("sha256"):
                raise OrchestratorContractError(
                    "dependency_output_hash_mismatch",
                    f"dependency output changed after completion: {path}",
                )

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
    release_global_gpu = False
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        state = _validate_claim(run, task_id, claim_token)
        task = _task_map(plan)[task_id]
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
            release_global_gpu = True
        elif gpu_minutes is not None:
            raise OrchestratorContractError(
                "gpu_minutes_unexpected", "CPU task cannot report GPU minutes"
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
