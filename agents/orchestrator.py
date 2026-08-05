"""Audited task-state Orchestrator for NovaPeptide execution plans.

Orchestrator validates an immutable Planner plan plus digest-bound approvals,
tracks task dependencies, issues worker claim packets, enforces a single-GPU
lease, and records hashed outputs.  It deliberately does not spawn Design or
Prediction processes in v1.  A human or AI worker claims a ready task, follows
the dispatch packet, and reports completion/failure through this module.

No task that requests approval can be claimed without an approval artifact
covering the exact plan SHA-256, task ID, and resource ceilings.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt  # type: ignore


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.planner import (  # noqa: E402
    APPROVAL_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    PLANNER_VERSION,
    PlannerContractError,
    _validate_plan_for_approval,
)
import data_layer  # noqa: E402
from data_layer import EvidenceLogger, State  # noqa: E402
from prediction_pipeline.contracts import file_sha256, object_sha256  # noqa: E402
from contracts.artifact import ArtifactRef  # noqa: E402
from contracts.errors import ErrorInfo  # noqa: E402
from contracts.task import (  # noqa: E402
    MUTABLE_TASK_STATUSES,
    SUCCESS_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
)
from contracts.trace import TraceContext, derive_workflow_id  # noqa: E402
from execution.contracts import DISPATCH_SCHEMA_VERSION  # noqa: E402


ORCHESTRATOR_VERSION = "1.1.1"
RUN_SCHEMA_VERSION = 2
LEGACY_RUN_SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^orchestrator_[0-9a-f]{12}$")


class OrchestratorContractError(ValueError):
    """Plan, approval, run state, claim, resource, or output is unsafe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestratorContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorContractError(
            f"{label}_malformed", f"invalid JSON in {path}"
        ) from exc
    if not isinstance(value, dict):
        raise OrchestratorContractError(f"{label}_type", f"{label} must be an object")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def _exclusive_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write("0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _run_lock(run_path: Path):
    lock_path = run_path.with_name(f".{run_path.name}.lock")
    with _exclusive_file_lock(lock_path):
        yield


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


def _finite_nonnegative(value: Any, code: str, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OrchestratorContractError(code, f"{label} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise OrchestratorContractError(code, f"{label} must be finite and non-negative")
    return number


def _load_plan(plan_path: str | Path) -> tuple[Path, dict, str]:
    path = Path(plan_path).expanduser().resolve()
    plan = _read_json(path, "planner_plan")
    try:
        plan = _validate_plan_for_approval(plan, path)
    except (PlannerContractError, OSError) as exc:
        raise OrchestratorContractError(
            getattr(exc, "code", "planner_plan_invalid"), str(exc)
        ) from exc
    return path, plan, file_sha256(path)


def _task_map(plan: dict) -> dict[str, dict]:
    return {task["task_id"]: task for task in plan["tasks"]}


def _workflow_id_for_plan(plan: dict) -> str:
    """Read the Planner workflow ID, deriving one only for legacy plans."""
    source = plan.get("source") or {}
    project_id = str(source.get("project_id") or "unknown_project")
    workflow_id = plan.get("workflow_id") or source.get("workflow_id")
    if workflow_id:
        try:
            TraceContext(project_id=project_id, workflow_id=str(workflow_id))
        except ValueError as exc:
            raise OrchestratorContractError(
                "workflow_id_invalid", "Planner workflow_id is invalid"
            ) from exc
        return str(workflow_id)
    source_id = str(source.get("critic_report_id") or plan.get("plan_id") or "plan")
    source_sha256 = str(
        source.get("critic_report_sha256")
        or plan.get("input_digest")
        or "0" * 64
    )
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        source_sha256 = object_sha256(plan)
    return derive_workflow_id(
        project_id,
        source_id,
        source_sha256,
        (plan.get("cycle") or {}).get("source_round", 1),
    )


def _trace_for_run(
    run: dict,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
) -> TraceContext:
    plan_ref = run.get("plan") or {}
    project_id = str(plan_ref.get("project_id") or "unknown_project")
    workflow_id = str(run.get("workflow_id") or plan_ref.get("workflow_id") or "")
    if not workflow_id:
        workflow_id = _workflow_id_for_plan({
            "workflow_id": workflow_id or None,
            "source": {
                "project_id": project_id,
                "workflow_id": workflow_id or None,
                "critic_report_id": plan_ref.get("plan_id"),
                "critic_report_sha256": plan_ref.get("plan_sha256"),
            },
            "plan_id": plan_ref.get("plan_id"),
            "cycle": {"source_round": 1},
        })
    attempt_id = None
    if task_id is not None and attempt is not None:
        attempt_id = TraceContext.attempt_id_for(task_id, attempt)
    return TraceContext(
        project_id=project_id,
        workflow_id=workflow_id,
        run_id=run.get("run_id"),
        plan_id=plan_ref.get("plan_id"),
        task_id=task_id,
        attempt_id=attempt_id,
    )


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

    task_ids = semantic.get("approved_task_ids")
    if not isinstance(task_ids, list) or not task_ids or len(task_ids) != len(set(task_ids)):
        raise OrchestratorContractError(
            "approval_scope_invalid", "approval task IDs must be a non-empty unique array"
        )
    tasks = _task_map(plan)
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

    gpu_tasks = [
        tasks[task_id] for task_id in task_ids
        if tasks[task_id]["resource_request"]["class"] == "gpu"
    ]
    limits = semantic["budget_limits"]
    if gpu_tasks:
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


def _refresh(run: dict, plan: dict) -> None:
    before = (
        run.get("status"),
        tuple(sorted(
            (task_id, value.get("status"))
            for task_id, value in (run.get("tasks") or {}).items()
        )),
    )
    plan_tasks = _task_map(plan)
    states = run["tasks"]
    for task_id, state in states.items():
        if state["status"] in TERMINAL_TASK_STATUSES or state["status"] == TaskStatus.CLAIMED.value:
            continue
        task = plan_tasks[task_id]
        if task["execution_gate"]["status"] == "blocked":
            state["status"] = TaskStatus.BLOCKED.value
            continue
        if task["approval"]["required"] and _authorization_for_task(run, task_id) is None:
            state["status"] = TaskStatus.AWAITING_APPROVAL.value
            continue
        dependency_statuses = [states[value]["status"] for value in task["depends_on"]]
        if any(value in {
            TaskStatus.FAILED.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.BLOCKED_DEPENDENCY.value,
        } for value in dependency_statuses):
            state["status"] = TaskStatus.BLOCKED_DEPENDENCY.value
        elif all(value in SUCCESS_TASK_STATUSES for value in dependency_statuses):
            state["status"] = TaskStatus.READY.value
        else:
            state["status"] = TaskStatus.PENDING_DEPENDENCY.value

    required_ids = [
        task["task_id"] for task in plan["tasks"] if task["disposition"] != "optional"
    ]
    optional_ids = [
        task["task_id"] for task in plan["tasks"] if task["disposition"] == "optional"
    ]
    required_statuses = [states[value]["status"] for value in required_ids]
    optional_statuses = [states[value]["status"] for value in optional_ids]
    if required_statuses and all(value == TaskStatus.SUCCEEDED.value for value in required_statuses):
        run["status"] = (
            "completed" if all(value in TERMINAL_TASK_STATUSES for value in optional_statuses)
            else "completed_required"
        )
    elif not required_statuses and all(
        value in TERMINAL_TASK_STATUSES for value in optional_statuses
    ):
        run["status"] = "completed"
    elif any(value == TaskStatus.FAILED.value for value in required_statuses):
        run["status"] = "failed"
    elif any(value["status"] == TaskStatus.CLAIMED.value for value in states.values()):
        run["status"] = "running"
    elif any(value["status"] == TaskStatus.READY.value for value in states.values()):
        run["status"] = "ready"
    elif any(value == TaskStatus.AWAITING_APPROVAL.value for value in required_statuses):
        run["status"] = "awaiting_approval"
    elif any(value in {
        TaskStatus.BLOCKED.value, TaskStatus.BLOCKED_DEPENDENCY.value
    } for value in required_statuses):
        run["status"] = "blocked"
    else:
        run["status"] = "pending"
    after = (
        run.get("status"),
        tuple(sorted(
            (task_id, value.get("status")) for task_id, value in states.items()
        )),
    )
    if after != before:
        run["updated_at"] = _utcnow()


def _run_summary(run: dict) -> dict:
    counts: dict[str, int] = {}
    for state in run["tasks"].values():
        counts[state["status"]] = counts.get(state["status"], 0) + 1
    return {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "run_id": run["run_id"],
        "workflow_id": run.get("workflow_id") or run["plan"].get("workflow_id"),
        "run_path": run["run_path"],
        "plan_id": run["plan"]["plan_id"],
        "plan_sha256": run["plan"]["plan_sha256"],
        "status": run["status"],
        "task_status_counts": dict(sorted(counts.items())),
        "gpu_lease": run["resources"].get("gpu_lease"),
        "gpu_minutes_consumed": run["resources"].get("gpu_minutes_consumed", 0.0),
    }


def _sync_state(run: dict, plan: dict) -> None:
    summary = _run_summary(run)
    patches: dict[str, Any] = {"orchestrator": summary}
    completed_required = run["status"] in {"completed_required", "completed"}
    if completed_required:
        current_round = int(State.load().get("round") or 1)
        source_round = int(plan["cycle"]["source_round"])
        target_round = int(plan["cycle"]["target_round"])
        if current_round not in {source_round, target_round}:
            raise OrchestratorContractError(
                "state_round_conflict",
                f"State round {current_round} conflicts with plan cycle "
                f"{source_round}->{target_round}",
            )
        patches["round"] = max(current_round, target_round)
        if any(
            task["agent"] == "critic"
            and run["tasks"][task["task_id"]]["status"] == TaskStatus.SUCCEEDED.value
            for task in plan["tasks"]
        ):
            patches["phase"] = "critic"
        elif any(
            task["agent"] == "reporter"
            and run["tasks"][task["task_id"]]["status"] == TaskStatus.SUCCEEDED.value
            for task in plan["tasks"]
        ):
            patches["phase"] = "report"
        else:
            patches["phase"] = "iterate"
    State.update(patches)
    history = State.load().get("iteration_history") or []
    history_status = "completed" if completed_required else "initialized"
    if not any(
        entry.get("agent") == "orchestrator"
        and (entry.get("summary") or {}).get("run_id") == run["run_id"]
        and (entry.get("summary") or {}).get("history_status") == history_status
        for entry in history
    ):
        history_summary = dict(summary, history_status=history_status)
        State.append_history({
            "phase": patches.get("phase", State.load().get("phase", "iterate")),
            "agent": "orchestrator",
            "summary": history_summary,
        })


def _upgrade_legacy_run(run: dict, plan: dict) -> None:
    """Adapt a v1 run in memory; the immutable plan digest remains untouched."""
    if run.get("schema_version") != LEGACY_RUN_SCHEMA_VERSION:
        return
    plan_ref = run.get("plan") or {}
    expected_workflow_id = _workflow_id_for_plan(plan)
    declared_ids = {
        value for value in (run.get("workflow_id"), plan_ref.get("workflow_id"))
        if value is not None
    }
    if declared_ids and declared_ids != {expected_workflow_id}:
        raise OrchestratorContractError(
            "workflow_id_mismatch", "legacy run workflow_id differs from Planner plan"
        )
    upgraded = json.loads(json.dumps(run))
    upgraded["schema_version"] = RUN_SCHEMA_VERSION
    upgraded["workflow_id"] = expected_workflow_id
    upgraded.setdefault("plan", {})["workflow_id"] = expected_workflow_id
    run.clear()
    run.update(upgraded)


def _validate_run_binding(run: dict, run_path: Path) -> tuple[Path, dict, str]:
    if run.get("schema_version") not in {LEGACY_RUN_SCHEMA_VERSION, RUN_SCHEMA_VERSION}:
        raise OrchestratorContractError("run_schema_unsupported", "unsupported run schema")
    if run.get("orchestrator_version") != ORCHESTRATOR_VERSION:
        raise OrchestratorContractError(
            "run_version_unsupported", "run belongs to another Orchestrator version"
        )
    if run.get("run_path") != str(run_path):
        raise OrchestratorContractError("run_path_mismatch", "run path binding differs")
    plan_ref = run.get("plan")
    if not isinstance(plan_ref, dict):
        raise OrchestratorContractError("run_plan_invalid", "run has no plan binding")
    plan_path, plan, plan_sha = _load_plan(plan_ref.get("plan_path"))
    if plan_sha != plan_ref.get("plan_sha256") or plan.get("plan_id") != plan_ref.get("plan_id"):
        raise OrchestratorContractError(
            "run_plan_hash_mismatch", "Planner plan changed after run initialization"
        )
    _upgrade_legacy_run(run, plan)
    plan_ref = run["plan"]
    expected_workflow_id = _workflow_id_for_plan(plan)
    if (
        run.get("workflow_id") != expected_workflow_id
        or plan_ref.get("workflow_id") != expected_workflow_id
    ):
        raise OrchestratorContractError(
            "workflow_id_mismatch", "run workflow_id differs from canonical Planner plan"
        )
    expected_run_id = f"orchestrator_{object_sha256({'plan_sha256': plan_sha, 'orchestrator_version': ORCHESTRATOR_VERSION})[:12]}"
    if run.get("run_id") != expected_run_id or not RUN_ID_RE.fullmatch(run.get("run_id", "")):
        raise OrchestratorContractError("run_id_mismatch", "run ID is not bound to plan")
    if set(run.get("tasks", {})) != set(_task_map(plan)):
        raise OrchestratorContractError("run_tasks_mismatch", "run task set differs from plan")
    return plan_path, plan, plan_sha


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


def initialize(
    *,
    plan_path: str | Path,
    approval_paths: Iterable[str | Path] = (),
    output_path: str | Path | None = None,
) -> dict:
    """Create or idempotently reopen an Orchestrator run."""
    resolved_plan_path, plan, plan_sha = _load_plan(plan_path)
    state_project = str(State.load().get("project_id") or "")
    plan_project = str((plan.get("source") or {}).get("project_id") or "")
    if state_project and plan_project and state_project != plan_project:
        raise OrchestratorContractError(
            "orchestrator_project_mismatch", "State and Planner plan projects differ"
        )
    run_id = f"orchestrator_{object_sha256({'plan_sha256': plan_sha, 'orchestrator_version': ORCHESTRATOR_VERSION})[:12]}"
    workflow_id = _workflow_id_for_plan(plan)
    active = State.load().get("orchestrator") or {}
    if (
        isinstance(active, dict)
        and active.get("run_id")
        and active.get("run_id") != run_id
        and active.get("status") not in {"completed", "failed"}
    ):
        raise OrchestratorContractError(
            "active_run_conflict",
            f"active Orchestrator run {active['run_id']} must finish before a new plan",
        )
    if output_path is None:
        output_path = resolved_plan_path.parent / "orchestrator" / run_id / "orchestrator_run.json"
    run_path = Path(output_path).expanduser().resolve()
    with _run_lock(run_path):
        if run_path.exists():
            run = _read_json(run_path, "orchestrator_run")
            bound_plan_path, bound_plan, bound_sha = _validate_run_binding(run, run_path)
            if bound_sha != plan_sha or bound_plan_path != resolved_plan_path:
                raise OrchestratorContractError(
                    "run_output_conflict", "run path is already bound to another plan"
                )
            plan = bound_plan
        else:
            now = _utcnow()
            run = {
                "schema_version": RUN_SCHEMA_VERSION,
                "orchestrator_version": ORCHESTRATOR_VERSION,
                "run_id": run_id,
                "workflow_id": workflow_id,
                "run_path": str(run_path),
                "plan": {
                    "plan_id": plan["plan_id"],
                    "plan_path": str(resolved_plan_path),
                    "plan_sha256": plan_sha,
                    "project_id": plan_project or None,
                    "workflow_id": workflow_id,
                },
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "approvals": [],
                "tasks": {
                    task["task_id"]: {
                        "status": TaskStatus.PENDING_DEPENDENCY.value,
                        "attempts": 0,
                        "claim": None,
                        "outputs": [],
                        "resource_usage": {},
                        "last_error": None,
                        "completed_at": None,
                        "attempt_history": [],
                    }
                    for task in plan["tasks"]
                },
                "resources": {
                    "gpu_lease": None,
                    "gpu_minutes_consumed": 0.0,
                    "gpu_minutes_by_approval": {},
                },
            }
        # Legacy plans/runs may not carry workflow_id yet.  Derive the adapter
        # once at this boundary and persist it so Worker never invents a new
        # trace identity.
        run.setdefault("workflow_id", workflow_id)
        run.setdefault("plan", {}).setdefault("workflow_id", workflow_id)
        added_approvals = []
        for approval_path in approval_paths:
            approval, added = _add_approval_in_memory(
                run,
                approval_path,
                plan_path=resolved_plan_path,
                plan=plan,
                plan_sha256=plan_sha,
            )
            if added:
                added_approvals.append(approval)
        _refresh(run, plan)
        _atomic_json(run_path, run)

    _sync_state(run, plan)
    if not any(
        event.get("event_type") == "orchestrator_run_initialized"
        and event.get("run_id") == run_id
        for event in EvidenceLogger.get_all()
    ):
        EvidenceLogger.log("orchestrator", "orchestrator_run_initialized", {
            "run_id": run_id,
            "run_path": str(run_path),
            "run_sha256": file_sha256(run_path),
            "plan_id": plan["plan_id"],
            "plan_sha256": plan_sha,
            "status": run["status"],
        }, phase="iterate", trace_context=_trace_for_run(run))
    for approval in added_approvals:
        EvidenceLogger.log("orchestrator", "orchestrator_approval_loaded", {
            "run_id": run_id,
            "approval_id": approval["approval_id"],
            "approval_sha256": approval["approval_sha256"],
            "approved_task_ids": approval["approved_task_ids"],
        }, phase="iterate", trace_context=_trace_for_run(run))
    return {"run": run, "run_path": str(run_path), "run_sha256": file_sha256(run_path)}


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


def complete(
    *,
    run_path: str | Path,
    task_id: str,
    claim_token: str,
    output_paths: Iterable[str | Path],
    gpu_minutes: float | None = None,
    transaction_managed: bool = False,
) -> dict:
    """Complete a claimed task after hashing outputs and checking GPU usage.

    When ``transaction_managed=True`` the legacy output inventory diff
    validation is skipped, because the transaction boundary (CommitManager)
    has already guaranteed artifact/candidate/state consistency atomically.
    """
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
        if not transaction_managed:
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


def status(*, run_path: str | Path) -> dict:
    """Read-only status snapshot after revalidating the immutable plan binding."""
    run_path = Path(run_path).expanduser().resolve()
    run = _read_json(run_path, "orchestrator_run")
    _, plan, _ = _validate_run_binding(run, run_path)
    snapshot = json.loads(json.dumps(run))
    _refresh(snapshot, plan)
    return {
        "run": snapshot,
        "run_path": str(run_path),
        "run_sha256": file_sha256(run_path),
        "summary": _run_summary(snapshot),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize/reopen a run")
    init.add_argument("--plan", required=True)
    init.add_argument("--approval", action="append", default=[])
    init.add_argument("--output")

    auth = commands.add_parser("authorize", help="attach one approval")
    auth.add_argument("--run", required=True)
    auth.add_argument("--approval", required=True)

    claim_cmd = commands.add_parser("claim", help="claim one ready task")
    claim_cmd.add_argument("--run", required=True)
    claim_cmd.add_argument("--task", required=True)
    claim_cmd.add_argument("--worker", required=True)

    complete_cmd = commands.add_parser("complete", help="complete one claimed task")
    complete_cmd.add_argument("--run", required=True)
    complete_cmd.add_argument("--task", required=True)
    complete_cmd.add_argument("--claim-token", required=True)
    complete_cmd.add_argument("--output", action="append", dest="outputs", required=True)
    complete_cmd.add_argument("--gpu-minutes", type=float)

    fail_cmd = commands.add_parser("fail", help="fail one claimed task")
    fail_cmd.add_argument("--run", required=True)
    fail_cmd.add_argument("--task", required=True)
    fail_cmd.add_argument("--claim-token", required=True)
    fail_cmd.add_argument("--reason", required=True)
    fail_cmd.add_argument("--retryable", action="store_true")
    fail_cmd.add_argument("--gpu-minutes", type=float)

    skip_cmd = commands.add_parser("skip", help="skip one optional task")
    skip_cmd.add_argument("--run", required=True)
    skip_cmd.add_argument("--task", required=True)
    skip_cmd.add_argument("--reason", required=True)

    recover_cmd = commands.add_parser("recover", help="close an abandoned claim")
    recover_cmd.add_argument("--run", required=True)
    recover_cmd.add_argument("--task", required=True)
    recover_cmd.add_argument("--claim-token", required=True)
    recover_cmd.add_argument("--operator", required=True)
    recover_cmd.add_argument("--reason", required=True)
    recover_cmd.add_argument("--confirmed-process-stopped", action="store_true")
    recover_cmd.add_argument("--gpu-minutes", type=float)

    status_cmd = commands.add_parser("status", help="show run status")
    status_cmd.add_argument("--run", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = initialize(
                plan_path=args.plan,
                approval_paths=args.approval,
                output_path=args.output,
            )
        elif args.command == "authorize":
            result = authorize(run_path=args.run, approval_path=args.approval)
        elif args.command == "claim":
            result = claim(run_path=args.run, task_id=args.task, worker=args.worker)
        elif args.command == "complete":
            result = complete(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                output_paths=args.outputs,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "fail":
            result = fail(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                reason=args.reason,
                retryable=args.retryable,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "skip":
            result = skip(run_path=args.run, task_id=args.task, reason=args.reason)
        elif args.command == "recover":
            result = recover(
                run_path=args.run,
                task_id=args.task,
                claim_token=args.claim_token,
                operator=args.operator,
                reason=args.reason,
                process_stopped_confirmed=args.confirmed_process_stopped,
                gpu_minutes=args.gpu_minutes,
            )
        elif args.command == "status":
            result = status(run_path=args.run)
        else:
            raise AssertionError(args.command)
    except (OrchestratorContractError, PlannerContractError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    run = result["run"]
    print(json.dumps({
        "status": "complete",
        "run_id": run["run_id"],
        "run_status": run["status"],
        "run_path": result["run_path"],
        "task_statuses": {
            task_id: value["status"] for task_id, value in run["tasks"].items()
        },
        **({
            "task_id": result["task_id"],
            "claim_token": result.get("claim_token"),
            "dispatch_packet_path": result.get("dispatch_packet_path"),
        } if "task_id" in result else {}),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
