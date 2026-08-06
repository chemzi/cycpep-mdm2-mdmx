"""service - split from agents/orchestrator.py (PR6)."""

from __future__ import annotations

import json, re
from contracts.plan import PlanContractError, validate_plan_for_approval
from contracts.task import SUCCESS_TASK_STATUSES, TERMINAL_TASK_STATUSES, TaskStatus
from contracts.trace import TraceContext, derive_workflow_id
from data_layer import EvidenceLogger, State
from pathlib import Path
from prediction_pipeline.contracts import file_sha256, object_sha256
from typing import Any, Iterable
from .approval import _add_approval_in_memory, _authorization_for_task, _task_map
from .config import (
    LEGACY_RUN_SCHEMA_VERSION,
    ORCHESTRATOR_VERSION,
    RUN_ID_RE,
    RUN_SCHEMA_VERSION,
)
from .errors import OrchestratorContractError
from .io import _atomic_json, _read_json, _run_lock, _utcnow

def _load_plan(plan_path: str | Path) -> tuple[Path, dict, str]:
    path = Path(plan_path).expanduser().resolve()
    plan = _read_json(path, "planner_plan")
    try:
        plan = validate_plan_for_approval(plan, path)
    except (PlanContractError, OSError) as exc:
        raise OrchestratorContractError(
            getattr(exc, "code", "planner_plan_invalid"), str(exc)
        ) from exc
    return path, plan, file_sha256(path)

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

def _resolve_plan_identity(plan: dict, plan_sha: str) -> tuple[str, str]:
    """Validate State/plan project agreement and derive run identity."""
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
    return run_id, workflow_id

def _load_or_create_run(
    run_path: Path,
    resolved_plan_path: Path,
    plan: dict,
    plan_sha: str,
    plan_project: str,
    workflow_id: str,
    run_id: str,
) -> tuple[dict, dict]:
    """Reopen an existing bound run or materialize a fresh one."""
    if run_path.exists():
        run = _read_json(run_path, "orchestrator_run")
        bound_plan_path, bound_plan, bound_sha = _validate_run_binding(run, run_path)
        if bound_sha != plan_sha or bound_plan_path != resolved_plan_path:
            raise OrchestratorContractError(
                "run_output_conflict", "run path is already bound to another plan"
            )
        return run, bound_plan
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
    return run, plan

def _load_approvals(
    run: dict,
    plan: dict,
    resolved_plan_path: Path,
    plan_sha: str,
    approval_paths: Iterable[str | Path],
) -> list[dict]:
    """Bind each approval file into the run; return the newly added ones."""
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
    return added_approvals

def initialize(
    *,
    plan_path: str | Path,
    approval_paths: Iterable[str | Path] = (),
    output_path: str | Path | None = None,
) -> dict:
    """Create or idempotently reopen an Orchestrator run."""
    resolved_plan_path, plan, plan_sha = _load_plan(plan_path)
    run_id, workflow_id = _resolve_plan_identity(plan, plan_sha)
    if output_path is None:
        output_path = resolved_plan_path.parent / "orchestrator" / run_id / "orchestrator_run.json"
    run_path = Path(output_path).expanduser().resolve()
    with _run_lock(run_path):
        run, plan = _load_or_create_run(
            run_path,
            resolved_plan_path,
            plan,
            plan_sha,
            str((plan.get("source") or {}).get("project_id") or ""),
            workflow_id,
            run_id,
        )
        # Legacy plans/runs may not carry workflow_id yet.  Derive the adapter
        # once at this boundary and persist it so Worker never invents a new
        # trace identity.
        run.setdefault("workflow_id", workflow_id)
        run.setdefault("plan", {}).setdefault("workflow_id", workflow_id)
        added_approvals = _load_approvals(
            run, plan, resolved_plan_path, plan_sha, approval_paths
        )
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
