"""claim - split from agents/orchestrator/state_machine.py (PR6)."""

from __future__ import annotations

import uuid
from contracts.task import TaskStatus
from contracts.trace import TraceContext
from data_layer import EvidenceLogger, State
from execution.contracts import DISPATCH_SCHEMA_VERSION
from pathlib import Path
from prediction_pipeline.contracts import file_sha256
from .errors import OrchestratorContractError
from .io import _atomic_json, _read_json, _run_lock, _utcnow
from .lease import _acquire_global_gpu_lease, _release_global_gpu_lease
from .service import (
    _authorization_for_task,
    _refresh,
    _sync_state,
    _task_map,
    _trace_for_run,
    _validate_run_binding,
    _workflow_id_for_plan,
)

def _resolve_workflow(run: dict, plan: dict) -> None:
    """Bind and persist the run's workflow identity."""
    workflow_id = run.get("workflow_id") or _workflow_id_for_plan(plan)
    run["workflow_id"] = workflow_id
    run.setdefault("plan", {}).setdefault("workflow_id", workflow_id)

def _validate_round(run: dict, plan: dict) -> None:
    """State round must still match the plan's source/target rounds."""
    current_round = int(State.load().get("round") or 1)
    allowed_rounds = {int(plan["cycle"]["source_round"])}
    if run.get("status") in {"completed_required", "completed"}:
        allowed_rounds.add(int(plan["cycle"]["target_round"]))
    if current_round not in allowed_rounds:
        raise OrchestratorContractError(
            "state_round_conflict", "State round changed after planning"
        )

def _validate_claim_request(
    run: dict, plan: dict, task_id: str
) -> tuple[dict, dict | None]:
    """Pre-flight checks for one ready task: known, executable, approved."""
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
    return task, approval

def _apply_claim(
    run: dict,
    run_path: Path,
    task_id: str,
    state: dict,
    task: dict,
    attempt: int,
    token: str,
    worker: str,
    claimed_at: str,
    approval: dict | None,
) -> dict | None:
    """Mutate task state and persist the GPU lease for a claim; return lease."""
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
    return global_lease

def _build_dispatch_packet(
    run: dict,
    plan: dict,
    task: dict,
    task_id: str,
    attempt: int,
    token: str,
    worker: str,
    claimed_at: str,
    approval: dict | None,
) -> dict:
    """Assemble the immutable worker dispatch packet for one claim."""
    dependency_outputs = {
        dependency: run["tasks"][dependency]["outputs"]
        for dependency in task["depends_on"]
    }
    return {
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

def claim(*, run_path: str | Path, task_id: str, worker: str) -> dict:
    """Claim one ready task and emit an immutable worker dispatch packet."""
    run_path = Path(run_path).expanduser().resolve()
    worker = str(worker or "").strip()
    if not worker:
        raise OrchestratorContractError("worker_required", "worker identity is required")
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        _resolve_workflow(run, plan)
        active = State.load().get("orchestrator") or {}
        if isinstance(active, dict) and active.get("run_id") not in {None, run["run_id"]}:
            raise OrchestratorContractError(
                "active_run_conflict", "State points to a different Orchestrator run"
            )
        _validate_round(run, plan)
        task, approval = _validate_claim_request(run, plan, task_id)
        state = run["tasks"][task_id]
        token = uuid.uuid4().hex
        attempt = state["attempts"] + 1
        claimed_at = _utcnow()
        global_lease = _apply_claim(
            run,
            run_path,
            task_id,
            state,
            task,
            attempt,
            token,
            worker,
            claimed_at,
            approval,
        )
        packet = _build_dispatch_packet(
            run, plan, task, task_id, attempt, token, worker, claimed_at, approval
        )
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
