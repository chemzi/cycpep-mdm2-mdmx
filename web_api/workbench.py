"""Read-only Frontend V2 workbench view over formal backend seams."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from agents.orchestrator import OrchestratorContractError, status
from contracts.action import get_action_spec
from contracts.plan import PlanContractError, validate_plan_for_approval
from contracts.trace import TraceContext
from execution.action_registry import handler_for
from .candidate_science import (
    _safe_content_link,
    is_structure_artifact,
    project_candidate_science,
)


DEFAULT_COLLECTION_LIMIT = 100
TRACE_FIELDS = (
    "project_id",
    "workflow_id",
    "run_id",
    "plan_id",
    "task_id",
    "attempt_id",
    "transaction_id",
    "candidate_id",
    "artifact_id",
    "parent_event_id",
)
_PATH_TEXT_RE = re.compile(r"(?:[A-Za-z]:[\\/]|file://|\\\\[^\\\s]+\\|(?:^|\s)/(?:[^/\s]+/)+)")


def _read_plan(plan_path: str | Path) -> dict[str, Any]:
    path = Path(plan_path).expanduser().resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    return validate_plan_for_approval(value, path)


def _collection(scope: str, items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    total = len(items)
    returned_items = items[:limit]
    returned = len(returned_items)
    return {
        "scope": scope,
        "total": total,
        "returned": returned,
        "truncated": returned < total,
        "items": returned_items,
    }


def _trace(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in TRACE_FIELDS if value.get(key) is not None}


def _protocol(value: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = value.get("protocol") or value.get("protocol_identity")
    nested = nested if isinstance(nested, Mapping) else {}
    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    name = nested.get("name") or nested.get("protocol_name") or value.get("protocol_name") or metadata.get("protocol_name")
    version = nested.get("version") or nested.get("protocol_version") or value.get("protocol_version") or metadata.get("protocol_version")
    integrity = (
        nested.get("integrity_identity")
        or nested.get("sha256")
        or nested.get("protocol_sha256")
        or value.get("protocol_sha256")
        or metadata.get("protocol_sha256")
    )
    if name is None and version is None and integrity is None:
        return None
    return {key: item for key, item in {
        "name": name,
        "version": version,
        "integrity_identity": integrity,
    }.items() if item is not None}


def _run_relation(value: Mapping[str, Any], current_run_id: str | None) -> str:
    run_id = value.get("run_id")
    if current_run_id is not None and run_id == current_run_id:
        return "current_run"
    if run_id:
        return "historical_run"
    return "unlinked"


def _display_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return fallback if not text or _PATH_TEXT_RE.search(text) else text


def _enrich_provenance(
    result: dict[str, Any], value: Mapping[str, Any], current_run_id: str | None
) -> dict[str, Any]:
    result["trace"] = _trace(value)
    result["run_relation"] = _run_relation(value, current_run_id)
    protocol = _protocol(value)
    if protocol:
        result["protocol"] = protocol
    return result


def _project_view(project_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
    targets = state.get("targets")
    target_ids = sorted(targets) if isinstance(targets, Mapping) else []
    return {
        "project_id": project_id,
        "name": state.get("project") or project_id,
        "targets": target_ids,
    }


def _candidate_view(value: Mapping[str, Any], current_run_id: str | None) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in (
            "candidate_id", "sequence", "source_route", "status", "final_status",
            "created_at", "updated_at", "associations",
        )
        if value.get(key) is not None
    }
    metrics = value.get("metrics")
    if isinstance(metrics, Mapping):
        result["metrics"] = dict(metrics)
    return _enrich_provenance(result, value, current_run_id)


def _evidence_view(value: Mapping[str, Any], current_run_id: str | None) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in (
            "event_id", "timestamp", "agent", "event_type", "phase", "round",
            "code", "component", "retryable", "targets", "blocks",
        )
        if value.get(key) is not None
    }
    if value.get("event_type") == "exploration_shortlist":
        result.update({
            key: value[key]
            for key in (
                "k", "n_evaluated", "n_passed", "shortlist", "calibration",
                "source_event_ids", "unmapped_metrics",
            )
            if key in value
        })
    if value.get("message") is not None:
        result["message"] = _display_text(
            value["message"], "Evidence includes an internal-only detail."
        )
    return _enrich_provenance(result, value, current_run_id)


def _artifact_view(value: Mapping[str, Any], current_run_id: str | None) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in (
            "artifact_id", "artifact_type", "role", "size_bytes", "sha256",
            "schema_version", "created_at", "producer_task_id",
        )
        if value.get(key) is not None
    }
    content_link = _safe_content_link(value)
    artifact_id = str(value.get("artifact_id") or "")
    if content_link is None and is_structure_artifact(value) and artifact_id:
        content_link = f"/api/v2/artifacts/{artifact_id}/content"
    if content_link:
        result["content_link"] = content_link
    input_ids = value.get("input_artifact_ids")
    if isinstance(input_ids, list):
        result["input_artifact_ids"] = list(input_ids)
    return _enrich_provenance(result, value, current_run_id)


def _transaction_view(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in ("transaction_id", "status", "created_at", "updated_at")
        if value.get(key) is not None
    }
    result.update(_trace(value))
    error = value.get("error")
    if isinstance(error, Mapping):
        result["error"] = {
            key: error[key]
            for key in ("code", "message", "component", "retryable")
            if error.get(key) is not None
        }
    return result


def _reason_codes(task: Mapping[str, Any], state: Mapping[str, Any], *, executable: bool, handler_available: bool) -> list[str]:
    reasons: list[str] = []
    gate = task.get("execution_gate")
    gate = gate if isinstance(gate, Mapping) else {}
    if gate.get("status") == "blocked":
        reasons.extend(str(item) for item in gate.get("block_reasons") or [])
        if not reasons:
            reasons.append("execution_gate_blocked")
    if not executable:
        reasons.append("action_not_executable")
    elif not handler_available:
        reasons.append("action_handler_unavailable")
    status_value = state.get("status")
    if status_value == "awaiting_approval":
        reasons.append("approval_required")
    elif status_value in {"pending_dependency", "blocked_dependency"}:
        reasons.append("dependency_unsatisfied")
    return list(dict.fromkeys(reasons))


def _task_view(task: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    action_name = str(task.get("action") or "")
    try:
        spec = get_action_spec(action_name)
        executable = bool(spec.executable)
        available = handler_for(spec.action) is not None
        action = {
            "name": spec.action.value,
            "executable": executable,
            "handler_available": available,
            "resource_class": spec.resource_class,
            "output_roles": list(spec.output_roles),
        }
    except ValueError:
        executable = False
        available = False
        action = {
            "name": action_name,
            "executable": False,
            "handler_available": False,
            "resource_class": None,
            "output_roles": [],
        }
    reasons = _reason_codes(task, state, executable=executable, handler_available=available)
    if not action_name:
        reasons.append("action_unknown")
    result = {
        "task_id": task.get("task_id"),
        "agent": task.get("agent"),
        "kind": task.get("kind"),
        "disposition": task.get("disposition"),
        "depends_on": list(task.get("depends_on") or []),
        "status": state.get("status"),
        "action": action,
        "availability": {
            "available": executable and available and not reasons,
            "reason_codes": list(dict.fromkeys(reasons)),
        },
        "approval": {
            "required": bool((task.get("approval") or {}).get("required")),
            "state": "awaiting" if state.get("status") == "awaiting_approval" else "satisfied_or_not_required",
        },
        "execution_gate": {
            "status": (task.get("execution_gate") or {}).get("status"),
        },
    }
    protocol = _protocol(task)
    if protocol:
        result["protocol"] = protocol
    return result


def _execution_view(task_id: str, state: Mapping[str, Any], transactions: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = int(state.get("attempts") or 0)
    attempt_id = TraceContext.attempt_id_for(task_id, attempts) if attempts else None
    related = [
        item for item in transactions
        if item.get("task_id") == task_id and item.get("attempt_id") == attempt_id
    ]
    transaction_visibility = related[-1].get("status") if related else (
        "not_yet_recorded" if state.get("status") in {"claimed", "running"} else "none"
    )
    error = state.get("last_error")
    safe_error = None
    if isinstance(error, Mapping):
        safe_error = {
            key: error[key]
            for key in ("code", "component", "retryable")
            if error.get(key) is not None
        }
        if error.get("message") is not None:
            safe_error["message"] = _display_text(
                error["message"], "Task execution failed with an internal-only detail."
            )
    claim = state.get("claim")
    claim = claim if isinstance(claim, Mapping) else {}
    return {
        "task_id": task_id,
        "status": state.get("status"),
        "attempts": attempts,
        "attempt_id": attempt_id,
        "worker_id": claim.get("worker"),
        "transaction_visibility": transaction_visibility,
        "error": safe_error,
    }


def _protocol_collection(*collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    protocols: list[dict[str, Any]] = []
    for items in collections:
        for item in items:
            protocol = item.get("protocol")
            if protocol and protocol not in protocols:
                protocols.append(protocol)
    return protocols


def _recovery_blockers(
    evidence: list[dict[str, Any]], transactions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    unresolved_events = {
        "execution_transaction_compensation_conflict",
        "execution_transaction_compensation_unresolved",
    }
    statuses = {item.get("transaction_id"): item.get("status") for item in transactions}
    blockers = []
    for item in evidence:
        transaction_id = item.get("trace", {}).get("transaction_id")
        reports_unresolved = (
            item.get("event_type") in unresolved_events
            or item.get("code") == "transaction_recovery_unresolved"
        )
        if (
            item.get("run_relation") != "current_run"
            or not reports_unresolved
            or statuses.get(transaction_id) == "ROLLED_BACK"
        ):
            continue
        blockers.append({
            "code": "transaction_compensation_unresolved",
            "scope": "transaction",
            "transaction_id": transaction_id,
            "summary": "Transaction compensation requires resolution.",
        })
    return blockers


def _unique_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    identities = set()
    for blocker in blockers:
        identity = tuple(blocker.get(key) for key in (
            "code", "scope", "workflow_id", "run_id", "task_id", "transaction_id"
        ))
        if identity not in identities:
            identities.add(identity)
            unique.append(blocker)
    return unique


def _execution_views(
    run: Mapping[str, Any],
    plan: Mapping[str, Any],
    transactions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    states = run.get("tasks") or {}
    for task in plan.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        task_state = states.get(task_id) or {}
        task_view = _task_view(task, task_state)
        execution = _execution_view(task_id, task_state, transactions)
        tasks.append(task_view)
        executions.append(execution)
        if execution["error"]:
            blockers.append({
                "code": execution["error"].get("code") or "task_failed",
                "scope": "task",
                "task_id": task_id,
                "summary": execution["error"].get("message") or "Task execution failed.",
            })
        reasons = task_view["availability"]["reason_codes"]
        if reasons:
            blockers.append({
                "code": reasons[0],
                "scope": "task",
                "task_id": task_id,
                "summary": "Task action is not currently available.",
            })
    for transaction in transactions:
        if transaction.get("status") == "COMPENSATION_CONFLICT":
            blockers.append({
                "code": "transaction_compensation_unresolved",
                "scope": "transaction",
                "transaction_id": transaction.get("transaction_id"),
                "summary": "Transaction compensation requires resolution.",
            })
    return tasks, executions, blockers


class WorkbenchReader:
    """Deep read module: joins formal authorities behind one browser interface."""

    def __init__(
        self,
        store,
        *,
        status_reader: Callable[..., Mapping[str, Any]] = status,
        plan_reader: Callable[[str | Path], Mapping[str, Any]] = _read_plan,
        artifact_bytes_reader: Callable[[str | Path], bytes] = lambda path: Path(path).read_bytes(),
    ):
        self._store = store
        self._status_reader = status_reader
        self._plan_reader = plan_reader
        self._artifact_bytes_reader = artifact_bytes_reader

    def _read_current_binding(self, state: Mapping[str, Any], project_id: str):
        active = state.get("orchestrator")
        active = active if isinstance(active, Mapping) else {}
        run_path = active.get("run_path")
        if not run_path:
            return None, None, None, [], [{
                "code": "no_current_run",
                "scope": "workflow",
                "summary": "No current workflow run is recorded for this project.",
            }]
        try:
            status_result = self._status_reader(run_path=run_path)
            run = dict(status_result["run"])
            plan_ref = run.get("plan")
            if not isinstance(plan_ref, Mapping):
                raise OrchestratorContractError("run_plan_invalid", "run has no plan binding")
            plan = dict(self._plan_reader(plan_ref.get("plan_path")))
            if plan_ref.get("project_id") != project_id:
                raise OrchestratorContractError(
                    "orchestrator_project_mismatch", "current project differs from run"
                )
            if plan.get("workflow_id") != run.get("workflow_id"):
                raise OrchestratorContractError("workflow_id_mismatch", "plan differs from run")
            transactions = list(self._store.list_transactions(
                workflow_id=run.get("workflow_id"), run_id=run.get("run_id")
            ))
            workflow = {
                "workflow_id": run.get("workflow_id"),
                "plan_id": plan.get("plan_id"),
                "status": run.get("status"),
            }
            return workflow, run, plan, transactions, []
        except (
            OrchestratorContractError, PlanContractError, OSError,
            json.JSONDecodeError, KeyError, TypeError, ValueError,
        ):
            return None, None, None, [], [{
                "code": "workflow_binding_invalid",
                "scope": "workflow",
                "summary": "The current workflow binding is invalid; project data remains available.",
            }]

    def read(self, *, limit: int = DEFAULT_COLLECTION_LIMIT) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        project_id = str(self._store.project_id)
        state = self._store.get_state(project_id)
        candidates = [
            value for value in self._store.list()
            if value.get("project_id") in (None, project_id)
        ]
        evidence = list(self._store.query(project_id=project_id))
        artifacts = list(self._store.list_artifacts())
        workflow, run, plan, transaction_values, blockers = self._read_current_binding(
            state, project_id
        )

        current_run_id = run.get("run_id") if run else None
        project_transactions = list(self._store.list_transactions())
        projection = project_candidate_science(
            candidates=candidates,
            evidence=evidence,
            artifacts=artifacts,
            transactions=project_transactions,
            current_run_id=current_run_id,
            artifact_bytes_reader=self._artifact_bytes_reader,
        )
        candidate_views = [
            _candidate_view(value, current_run_id) for value in projection.candidates
        ]
        evidence_views = [_evidence_view(value, current_run_id) for value in evidence]
        artifact_views = []
        for value in artifacts:
            projected = dict(value)
            artifact_id = str(value.get("artifact_id") or "")
            candidate_ids = projection.artifact_candidates.get(artifact_id, ())
            if len(candidate_ids) == 1:
                projected["candidate_id"] = candidate_ids[0]
            role = projection.artifact_roles.get(artifact_id)
            if role:
                projected["role"] = role
            artifact_views.append(_artifact_view(projected, current_run_id))
        transaction_views = [_transaction_view(value) for value in transaction_values]

        task_views, execution_views, execution_blockers = (
            _execution_views(run, plan, transaction_views)
            if run is not None and plan is not None else ([], [], [])
        )
        blockers.extend(execution_blockers)
        blockers.extend(_recovery_blockers(evidence_views, transaction_views))

        protocols = _protocol_collection(task_views, candidate_views, evidence_views, artifact_views)
        return {
            "schema_version": "frontend.workbench.v2",
            "project": _project_view(project_id, state),
            "workflow": workflow,
            "run": None if run is None else {
                "run_id": run.get("run_id"),
                "workflow_id": run.get("workflow_id"),
                "plan_id": (run.get("plan") or {}).get("plan_id"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
            },
            "tasks": _collection("current_run", task_views, limit),
            "executions": _collection("current_run", execution_views, limit),
            "transactions": _collection("current_run", transaction_views, limit),
            "candidates": _collection("project", candidate_views, limit),
            "evidence": _collection("project", evidence_views, limit),
            "artifacts": _collection("project", artifact_views, limit),
            "protocols": _collection("project", protocols, limit),
            "trace": {
                "project_id": project_id,
                "workflow_id": workflow.get("workflow_id") if workflow else None,
                "run_id": current_run_id,
            },
            "blockers": _collection("workbench", _unique_blockers(blockers), limit),
        }


__all__ = ["DEFAULT_COLLECTION_LIMIT", "WorkbenchReader"]
