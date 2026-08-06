"""completion - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

from contracts.artifact import ArtifactRef
from contracts.task import TaskStatus
from contracts.trace import TraceContext
from data_layer import EvidenceLogger
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from typing import Any, Iterable
from .errors import OrchestratorContractError
from .io import _atomic_json, _read_json, _run_lock, _utcnow
from .lease import _consume_gpu_usage, _release_global_gpu_lease
from .service import (
    _refresh,
    _sync_state,
    _task_map,
    _trace_for_run,
    _validate_run_binding,
)
from .state_machine import _validate_claim

def _inventory_outputs(
    values: Iterable[str | Path],
    *,
    producer_task_id: str | None = None,
    producer_attempt_id: str | None = None,
) -> list[dict]:
    inventory = []
    seen_paths: set[str] = set()
    for raw in values:
        text = str(raw)
        role = None
        path_text = text
        if "=" in text:
            possible_role, possible_path = text.split("=", 1)
            if possible_role and "/" not in possible_role and "\\" not in possible_role:
                role, path_text = possible_role, possible_path
        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            raise OrchestratorContractError(
                "task_output_invalid", f"task output must be an existing file: {path}"
            )
        if str(path) in seen_paths:
            raise OrchestratorContractError(
                "task_output_duplicate", f"duplicate task output: {path}"
            )
        seen_paths.add(str(path))
        artifact_type = role or path.name
        sha256 = file_sha256(path)
        artifact = ArtifactRef(
            artifact_id="artifact_" + object_sha256({
                "artifact_type": artifact_type,
                "path": str(path),
                "sha256": sha256,
                "producer_task_id": producer_task_id,
                "producer_attempt_id": producer_attempt_id,
            })[:12],
            artifact_type=artifact_type,
            path=str(path),
            sha256=sha256,
            producer_task_id=producer_task_id,
            producer_attempt_id=producer_attempt_id,
            schema_version=1,
        )
        inventory.append({
            "role": artifact_type,
            "size_bytes": path.stat().st_size,
            **artifact.to_dict(),
        })
    return inventory

def complete(
    *,
    run_path: str | Path,
    task_id: str,
    claim_token: str,
    output_paths: Iterable[str | Path],
    gpu_minutes: float | None = None,
) -> dict:
    """Complete a claimed task after hashing outputs and checking GPU usage."""
    run_path = Path(run_path).expanduser().resolve()
    output_values = list(output_paths)
    if not output_values:
        raise OrchestratorContractError(
            "task_output_required", "successful task completion requires an output file"
        )
    release_global_gpu = False
    with _run_lock(run_path):
        run = _read_json(run_path, "orchestrator_run")
        _, plan, _ = _validate_run_binding(run, run_path)
        state = _validate_claim(run, task_id, claim_token)
        task = _task_map(plan)[task_id]
        inventory = _inventory_outputs(
            output_values,
            producer_task_id=task_id,
            producer_attempt_id=TraceContext.attempt_id_for(
                task_id, int(state.get("attempts") or 0)
            ),
        )
        try:
            from execution.contracts import validate_output_inventory

            dependency_outputs = {
                dependency: run["tasks"][dependency]["outputs"]
                for dependency in task["depends_on"]
            }
            validate_output_inventory(
                task, inventory, dependency_outputs=dependency_outputs
            )
        except Exception as exc:
            if isinstance(exc, OrchestratorContractError):
                raise
            raise OrchestratorContractError(
                getattr(exc, "code", "task_output_contract_invalid"), str(exc)
            ) from exc
        usage: dict[str, Any] = {}
        if task["resource_request"]["class"] == "gpu":
            usage = _consume_gpu_usage(
                run, task_id, gpu_minutes, enforce_limit=True
            )
            lease = run["resources"].get("gpu_lease")
            if not lease or lease.get("claim_token") != claim_token:
                raise OrchestratorContractError(
                    "gpu_lease_mismatch", "GPU task no longer owns the single-GPU lease"
                )
            run["resources"]["gpu_lease"] = None
            release_global_gpu = True
        elif gpu_minutes is not None:
            raise OrchestratorContractError(
                "gpu_minutes_unexpected", "CPU task cannot report GPU minutes"
            )
        state.update({
            "status": TaskStatus.SUCCEEDED.value,
            "claim": None,
            "outputs": inventory,
            "resource_usage": usage,
            "completed_at": _utcnow(),
        })
        history = state.setdefault("attempt_history", [])
        if history:
            history[-1].update({
                "status": TaskStatus.SUCCEEDED.value,
                "completed_at": state["completed_at"],
                "outputs": inventory,
            })
        _refresh(run, plan)
        _atomic_json(run_path, run)
    if release_global_gpu:
        _release_global_gpu_lease(claim_token)
    _sync_state(run, plan)
    EvidenceLogger.log("orchestrator", "orchestrator_task_completed", {
        "run_id": run["run_id"],
        "task_id": task_id,
        "outputs": inventory,
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
