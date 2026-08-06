"""lease - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

import contextlib, data_layer, os
from pathlib import Path
from .errors import OrchestratorContractError
from .io import _atomic_json, _exclusive_file_lock, _finite_nonnegative, _read_json
from .service import _authorization_for_task

def _global_gpu_lease_path() -> Path:
    explicit = os.environ.get("CYCPEP_GPU_LEASE_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json"

@contextlib.contextmanager
def _global_gpu_lock():
    lease_path = _global_gpu_lease_path()
    lock_path = lease_path.with_name(f".{lease_path.name}.lock")
    with _exclusive_file_lock(lock_path):
        yield

def _acquire_global_gpu_lease(lease: dict) -> None:
    path = _global_gpu_lease_path()
    with _global_gpu_lock():
        if path.exists():
            current = _read_json(path, "global_gpu_lease")
            raise OrchestratorContractError(
                "gpu_lease_busy",
                f"global GPU lease is held by {current.get('run_id')}:{current.get('task_id')}",
            )
        _atomic_json(path, lease)

def _release_global_gpu_lease(claim_token: str) -> None:
    path = _global_gpu_lease_path()
    with _global_gpu_lock():
        current = _read_json(path, "global_gpu_lease")
        if current.get("claim_token") != claim_token:
            raise OrchestratorContractError(
                "global_gpu_lease_mismatch", "global GPU lease belongs to another claim"
            )
        path.unlink()

def _consume_gpu_usage(
    run: dict,
    task_id: str,
    gpu_minutes: float | None,
    *,
    enforce_limit: bool,
) -> dict:
    if gpu_minutes is None:
        raise OrchestratorContractError(
            "gpu_minutes_required", "GPU task outcome requires actual GPU minutes"
        )
    minutes = _finite_nonnegative(gpu_minutes, "gpu_minutes_invalid", "gpu_minutes")
    approval = _authorization_for_task(run, task_id)
    if approval is None:
        raise OrchestratorContractError(
            "task_approval_missing", "GPU task lost its approval"
        )
    approval_id = approval["approval_id"]
    consumed = float(
        run["resources"]["gpu_minutes_by_approval"].get(approval_id, 0.0)
    )
    limit = float(approval["budget_limits"]["max_gpu_minutes"])
    over_budget = consumed + minutes > limit + 1e-9
    if over_budget and enforce_limit:
        raise OrchestratorContractError(
            "gpu_minutes_exceeded",
            f"task outcome would exceed approval GPU ceiling {limit}",
        )
    run["resources"]["gpu_minutes_by_approval"][approval_id] = consumed + minutes
    run["resources"]["gpu_minutes_consumed"] = float(
        run["resources"].get("gpu_minutes_consumed", 0.0)
    ) + minutes
    return {
        "gpu_minutes": minutes,
        "approval_id": approval_id,
        "over_budget": over_budget,
        "approved_gpu_minutes": limit,
    }
